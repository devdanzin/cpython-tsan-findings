# Crash: a shared `set`/`frozenset` iterator double-DECREFs `si_set` in its exhaustion path under free-threading (`setiter_iternext`, `Objects/setobject.c:1131`)

*`setiter_iternext` reads `so = si->si_set` unguarded, takes only the **set's** critical section around the table scan, and then on exhaustion runs `si->si_set = NULL; Py_DECREF(so);` **outside** that section. Two threads advancing the same set iterator to exhaustion both read the same non-NULL `so` and both `Py_DECREF(so)` → the set's refcount underflows → **use-after-free**. This is the set-iterator sibling of the dict-iterator double-DECREF (TSAN-0053 / cpython#154130) and the memory-safety crash face of the value-benign set-iterator data race (TSAN-0040 / cpython#144356).*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducers and captured the backtrace; the maintainer reviewed and edited it._

## Summary

`Objects/setobject.c` (main@`a1d580430c8`):

```c
static PyObject *setiter_iternext(PyObject *self)
{
    setiterobject *si = (setiterobject*)self;
    PyObject *key = NULL;
    PySetObject *so = si->si_set;          /* :1101  plain read of the owning ref */
    if (so == NULL)
        return NULL;
    ...
    Py_BEGIN_CRITICAL_SECTION(so);         /* :1116  locks the SET, not the iterator */
    i = si->si_pos;
    entry = so->table;
    ... scan for the next key ...
    Py_END_CRITICAL_SECTION();             /* :1127 */
    si->si_pos = i+1;
    if (key == NULL) {                     /* exhausted */
        si->si_set = NULL;                 /* :1130  non-atomic clear */
        Py_DECREF(so);                     /* :1131  drop the iterator's owning ref to the set */
        return NULL;
    }
    si->len--;
    return key;
}
```

The iterator holds exactly **one** reference to the set (`si_set`). The critical section covers only the table scan; the exhaustion clear-and-DECREF at `:1130-1131` is outside it, and the caller-visible read of `si_set` (`:1101`) is unsynchronized. Two threads `T1`/`T2` calling `next()` on the same near-exhausted iterator interleave as:

- `T1`: `so = si->si_set` (non-NULL)
- `T2`: `so = si->si_set` (same non-NULL)
- both find `key == NULL` (exhausted) and both run `si->si_set = NULL; Py_DECREF(so)`

The second `Py_DECREF` has no matching reference → the set is freed one owner too early (or, in debug, its shared refcount underflows past zero).

## Reproducer

```python
import threading

NT = 8
ITERS = 200_000

def newit():
    return iter(set(range(32)))

cell = [newit()]

def worker():
    for _ in range(ITERS):
        it = cell[0]
        try:
            next(it)                 # setiter_iternext
        except StopIteration:
            cell[0] = newit()        # refill so the exhaustion path is hit repeatedly
        except Exception:
            pass

threads = [threading.Thread(target=worker) for _ in range(NT)]
for t in threads: t.start()
for t in threads: t.join()
print("done, no crash")
```

`PYTHON_GIL=0 ./python repro.py`:

- **`debug-ft-nojit` → SIGABRT within seconds, ~8/8**: `Objects/setobject.c:1131: _Py_NegativeRefcount: object has negative ref count` — the `Py_DECREF(so)` at `:1131`.
- **`release-ft-nojit-o0` (no sanitizer) → 6/6 crash** (SIGSEGV / SIGABRT, core dumped) — genuine UAF.

**Long-lived face (`repro_frozenset_gc.py`).** When the set is a long-lived shared `frozenset` (e.g. a module-level constant, refcount > 1), the double-DECREF doesn't go immediately negative — the underflow is silent until the next `gc.collect()`:

```
Python/gc_free_threading.c:999: update_refs: Assertion "refcount >= 0" failed
object type name: frozenset
object refcount : 1152921504606846947      <-- underflowed / deferred-refcount artifact
```

or a plain SIGSEGV (UAF) on a later `iter()`/access of the freed set. This is how the fuzzer surfaced it (a shared `frozenset` GC-assert).

## Crash backtrace (`debug-ft-nojit`, `PYTHON_GIL=0`)

```
#9  _Py_NegativeRefcount                              Objects/object.c:275
#11 _Py_DecRefSharedDebug (lineno=1131)               Objects/object.c:425
#12 Py_DECREF (lineno=1131)                           ./Include/refcount.h:363
#13 setiter_iternext (self=<set_iterator>)            Objects/setobject.c:1131   <-- Py_DECREF(so)
#14 builtin_next                                      Python/bltinmodule.c:1776
```

## Root cause

Identical to the dict iterator (TSAN-0053): a lock-free-ish `next()` whose exhaustion step is a non-atomic read-modify-write on a shared `PyObject *` (`si_set`) and the referent's refcount, with the critical section scoped to the *set* rather than the *iterator*. Safe under the GIL; under `--disable-gil` a shared iterator lets two threads both consume and drop the single owning reference.

## Impact / severity

**High (memory-unsafe, crashes).** A double-free / use-after-free of a live `set`/`frozenset`, reachable from pure Python on a free-threaded build. Per the iterator free-threading strategy ([gh-124397](https://github.com/python/cpython/issues/124397)) concurrent iteration may return duplicate/skipped values or raise, but it **must not crash**; this crashes. The long-lived-`frozenset` face is the realistic one — a module-level frozen set iterated from several threads corrupts memory silently until GC.

## Suggested fix

Serialize concurrent `next()` on the same iterator and drop the ref exactly once. **[cpython#144357](https://github.com/python/cpython/pull/144357)** (open PR by the #144356 author) already does this — it widens the lock to `Py_BEGIN_CRITICAL_SECTION2(self, so)` and, under `Py_GIL_DISABLED`, makes exhaustion sticky via `si_pos = -1` and **removes the `si_set = NULL; Py_DECREF(so)` from `iternext` entirely** (the set ref is dropped only in dealloc). That fixes this crash — but the PR has been **stalled since 2026-05**.

## Notes

- **Prior art / disposition.** [cpython#144356](https://github.com/python/cpython/issues/144356) (OPEN) reports only the *data race* (`__length_hint__`/cursor); its fix PR #144357 (OPEN) would also fix this crash but is stalled. Older *closed* #112069/#117935 ("Make `setiter_iternext` thread-safe") hardened the value path but left the `si_set` double-DECREF. So the **crash** is unreported as such. Because #144356 is **open with an active fix**, the right move is to **corroborate there** with this reproducer (demonstrating memory-unsafety, not just a benign race) to unstall the fix — as opposed to the dict case (TSAN-0053), where the analogous issue was *closed* so we filed a new one (cpython#154130).
- **Distinct from TSAN-0040** (the value-benign `si_pos` cursor race in the same function) and a direct sibling of **TSAN-0053** (dict iterator). `frozenset` is the long-lived / GC-caught face here, exactly as `frozendict` is for TSAN-0053.
- **Found via the `--tsan` un-masking profile (fleet-15)**, as a `frozenset` `validate_gc_objects`/`update_refs` abort (`tsanNOPARSE` — an abort/segfault, not a TSan data-race report).

---

*New crashing free-threading bug (double-free / UAF), set-iterator sibling of TSAN-0053. Fixed by the stalled cpython#144357; corroborate there.*
