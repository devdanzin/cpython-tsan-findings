# Data race: a shared `str` iterator advances `it_index` non-atomically (`unicode_ascii_iter_next` / `unicodeiter_next` vs `unicodeiter_len`, `Objects/unicodeobject.c`) — this is cpython#153928

*The str iterator keeps its cursor in a plain `it->it_index` and its source in `it->it_seq`, with no synchronization. When one str iterator is shared across threads, `unicode_ascii_iter_next` reads-and-writes `it->it_index` (`:14983`) with no atomicity while `unicodeiter_len` (the `__length_hint__` slot, `:14997`) plainly reads it — a data race on the cursor (and an out-of-bounds read past the string), plus an `it_seq = NULL; Py_DECREF(seq)` double-DECREF on exhaustion. This **is** [cpython#153928](https://github.com/python/cpython/issues/153928) (filed by johng); TSAN-0037 is the byte-for-byte-identical `bytes` form.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

CPython has two str-iterator `next` implementations sharing the `unicodeiterobject` layout: `unicode_ascii_iter_next` (the ASCII fast path) and `unicodeiter_next` (the general path). Both advance `it->it_index` with plain read-modify-writes, and `unicodeiter_len` (`__length_hint__`) plainly reads it:

```c
/* unicode_ascii_iter_next (ASCII) / unicodeiter_next (general) */
    ...
    it->it_index++;                                          /* :14983 (ascii)  WRITE it_index, no atomicity */
    ...
/* unicodeiter_len */
    len = PyUnicode_GET_LENGTH(seq) - it->it_index;          /* :14997  READ it_index for __length_hint__ */
```

A shared str iterator driven by `next()` from some threads and `operator.length_hint()` from others races on `it_index`. On exhaustion, two threads both running `it->it_seq = NULL; Py_DECREF(seq)` double-DECREF the source str. Fleet-10 confirmed the **general** (non-ASCII) `unicodeiter_next` path races identically — the ASCII/general distinction is only which `next` implementation runs; both have the same non-atomic `it_index`.

## Reproducer

```python
import operator
import sys
import threading

# str-iterator form of cpython#153928: many threads share ONE str iterator; half advance it
# (unicode_ascii_iter_next writes it->it_index, Objects/unicodeobject.c:14983) and half read its
# cursor via __length_hint__ (unicodeiter_len reads it->it_index, :14997) -> non-atomic it_index
# data race (+ it_seq double-DECREF on exhaustion, by inspection, same as the bytes analog
# TSAN-0037). Run under the debug-ft-nojit-tsan build with PYTHON_GIL=0; exit 66 = TSan race.
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NT = 8
LEN = 4096


def advance(it, bar):
    bar.wait()
    for _ in it:
        pass


def measure(it, bar):
    bar.wait()
    for _ in range(LEN):
        operator.length_hint(it, 0)


for _r in range(ROUNDS):
    shared = iter("A" * LEN)
    bar = threading.Barrier(NT)
    ts = [
        threading.Thread(target=(advance if i % 2 else measure), args=(shared, bar))
        for i in range(NT)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:exitcode=66:history_size=4" \
  setarch -R ./python repro.py
```

Deterministic, exit **66**. `SUMMARY` names `unicodeiter_len:14997` (read) vs `unicode_ascii_iter_next:14983` (write). (Full report in `tsan_report.txt`.)

## Root cause

`unicodeiterobject` (`it_index`, `it_seq`) is a builtin sequence iterator whose per-iteration state is mutated with no per-object critical section and no atomics — safe under the GIL, a data race when the iterator is shared in the free-threaded build. Both the ASCII (`unicode_ascii_iter_next`) and general (`unicodeiter_next`) `next` implementations have the identical non-atomic `it_index`, and `unicodeiter_len` reads it unguarded. The `it_seq` exhaustion path double-DECREFs.

## Impact / severity

**Moderate-to-high (memory-unsafe).** The `it_index` race is an OOB read past the string; the `it_seq` double-DECREF is a refcount underflow / UAF. Sharing one iterator across threads is unusual (lower real-world priority), but str declares no per-object locking. Already filed upstream (#153928). Free-threaded build only.

## Suggested fix

Same as the bytes analog / #153928: make `it_index` an atomic load/store (or take a per-iterator critical section over both `next` implementations and `unicodeiter_len`), and release `it_seq` exactly once via an atomic exchange on exhaustion. Apply to both the ASCII and general str-iterator paths (and the bytes/list/tuple/range iterators share the shape).

## Notes

- **Why this is a bug** (per CPython's iterator strategy). [gh-124397](https://github.com/python/cpython/issues/124397) ("Strategy for Iterators in Free Threading", Raymond Hettinger) sets the bar: C iterators get "only the minimal changes necessary to cause them to **not crash** … concurrent access is allowed to return duplicate values, skip values, or raise an exception." The pure `it_index` *value* race is therefore acceptable; the fileable parts are memory-unsafe — the OOB read past the string and the `it_seq` double-DECREF (UAF) — which is exactly why #153928 is being fixed.
- This **is** cpython#153928 (str `unicode_ascii_iter_next`), which we independently reproduced and commented on. `status: reported`.
- Fleet-10 folded the **general** (non-ASCII) `unicodeiter_next` faces (`unicodeiter_next | unicodeiter_next`, `unicodeiter_len | unicodeiter_next`) here — same bug/#153928, the non-ASCII code path.
- Same builtin-iterator shared-cursor family as TSAN-0037 (bytes), TSAN-0039 (struct / #154013), TSAN-0040 (set), TSAN-0026 (dict).
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 08, 55 vehicles): the op-mix shared-iterator path shares one `iter('A'*N)` across workers, some advancing via `next()` and some reading the cursor via `operator.length_hint`.

---

*This is cpython#153928 (str iterator). Recorded for the catalog; not a separate filing.*
