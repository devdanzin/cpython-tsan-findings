# Data race: `descr_get_qualname` lazily caches `descr->d_qualname` without a critical section → write/write race + leak on a shared descriptor (`Objects/descrobject.c:625`)

*Method / getset / wrapper descriptors cache their `__qualname__` lazily in `descr->d_qualname`. `descr_get_qualname` does `if (descr->d_qualname == NULL) descr->d_qualname = calculate_qualname(descr);` with **no** lock. Descriptors live on their owning type and are shared across all threads, so two threads first-reading the same descriptor's `__qualname__` both see `NULL`, both compute, and both **write** `descr->d_qualname` — a write/write data race and a leak (one computed str is overwritten and orphaned).*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Objects/descrobject.c`:

```c
static PyObject *
descr_get_qualname(PyObject *self, void *Py_UNUSED(ignored))
{
    PyDescrObject *descr = (PyDescrObject *)self;
    if (descr->d_qualname == NULL)
        descr->d_qualname = calculate_qualname(descr);   /* :625  WRITE, unlocked */
    return Py_XNewRef(descr->d_qualname);
}
```

`d_qualname` is a lazily-populated cache. The check-then-write is not atomic, and a descriptor is reachable from its type (which is shared), so `SomeType.some_method.__qualname__` read concurrently from two threads races: both take the `== NULL` branch, both `calculate_qualname`, both store — write/write on the pointer, plus a leaked qualname str for the losing store.

## Reproducer

```python
import threading
NT = 8
# gather many builtin method/getset/wrapper descriptors whose d_qualname is still NULL
descrs = []
for tp in (str, bytes, list, dict, set, int, float, tuple, frozenset, bytearray):
    for name, v in vars(tp).items():
        if type(v).__name__ in ("method_descriptor", "getset_descriptor", "wrapper_descriptor"):
            descrs.append(v)
for _round in range(50):
    for d in descrs:                      # each descriptor raced once (d_qualname caches after)
        b = threading.Barrier(NT)
        def w(dd=d, bb=b):
            bb.wait()
            for _ in range(20):
                _ = dd.__qualname__       # descr_get_qualname: lazy d_qualname write
        ts = [threading.Thread(target=w) for _ in range(NT)]
        for t in ts: t.start()
        for t in ts: t.join()
print("done")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:exitcode=66:history_size=4" \
  setarch -R ./python repro.py
```

Deterministic, exit **66**. Reproduces on **both** `debug-ft-nojit-tsan` and `release-ft-nojit-tsan`.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, fleet build `a1d580430c8`)

```
WARNING: ThreadSanitizer: data race
  Write of size 8 by thread T?:
    #0 descr_get_qualname   Objects/descrobject.c:625   (descr->d_qualname = calculate_qualname(descr))
  Previous write of size 8 by thread T?:
    #0 descr_get_qualname   Objects/descrobject.c:625
SUMMARY: ThreadSanitizer: data race Objects/descrobject.c:625 in descr_get_qualname
```

(Full report in `tsan_report.txt`.)

## Root cause

A lazily-initialized `PyObject*` cache field (`d_qualname`) on a shared object, populated through an unsynchronized `if (field == NULL) field = compute();`. This is the same free-threading defect class as the `object.__reduce_ex__` `objreduce` cache (gh-125267, fixed) and the `_elementtree` `extra` struct (TSAN-0041 / gh-149816): a check-then-create that isn't atomic across threads.

## Impact / severity

**Moderate.** Write/write on a heap pointer → leaks one qualname str per lost race, and a concurrent reader can observe a torn/overwritten `d_qualname`. Descriptors are attached to types, which are shared across threads, so concurrent first-access of `X.method.__qualname__` is realistic (any two threads introspecting the same class). Free-threaded build only.

## Suggested fix

Serialize the lazy init: `Py_BEGIN_CRITICAL_SECTION(descr)` around the check-and-compute (re-checking `d_qualname` inside), or make it a one-shot atomic compare-exchange — compute, then `CAS(&descr->d_qualname, NULL, new)`, DECREF the loser. Mirrors how other lazy `PyObject*` caches were hardened for free-threading (e.g. gh-125267 eagerly initializes `objreduce`).

## Notes

- **Appears genuinely unfiled** — a `gh api` tracker search for `calculate_qualname` / `d_qualname` / `descr_get_qualname` returned nothing. Fileable candidate; outward-facing, awaiting maintainer go-ahead.
- Same lazy-cache-without-lock class as gh-125267 (objreduce, fixed) and TSAN-0041 (`_elementtree` extra). Distinct from the iterator-cursor family.
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 11). It only surfaced once `--tsan-no-halt` (fleet 11's first multi-race fleet) stopped the dominant count/iterator races from masking it under `halt_on_error=1`.

---

*New lazy-cache write/write race; recorded for the catalog. Not yet filed.*
