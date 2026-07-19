# Data race (value-benign): a shared generic sequence iterator advances `it_index` non-atomically (`iter_iternext` vs `iter_len`, `Objects/iterobject.c`) — the gh-120496 class

*The generic sequence iterator (`seqiterobject` in `Objects/iterobject.c`, returned by `iter(obj)` for a `__getitem__` type and as the fallback iterator) keeps its cursor in a plain `it->it_index`. `iter_iternext` writes `it->it_index++` (`:72`) while `iter_len` reads it (`:100`); a shared iterator races the cursor. The `collections.deque` iterator is the same class. This **is** gh-120496 ("Sequence iterator thread-safety"), which was **closed** because the race is value-benign — a duplicate/skipped value, not a crash — and CPython's iterator strategy explicitly allows that.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Objects/iterobject.c`:

```c
static PyObject *
iter_iternext(PyObject *iterator)
{
    _PyListIterObject *it = ...;             /* seqiterobject: it_seq, it_index */
    PyObject *seq = it->it_seq;
    if (seq == NULL) return NULL;
    if (it->it_index == PY_SSIZE_T_MAX) { ... }
    result = PySequence_GetItem(seq, it->it_index);   /* read it_index */
    if (result != NULL) {
        it->it_index++;                               /* :72  WRITE it_index (non-atomic) */
        return result;
    }
    it->it_seq = NULL;                                /* exhaustion */
    ...
}

static PyObject *
iter_len(PyObject *op, ...)
{
    ...
    len = seqsize - it->it_index;                     /* :100  READ it_index (non-atomic) */
    ...
}
```

Two threads sharing one iterator — one advancing via `next()`, one reading `operator.length_hint()` — race on `it_index`. The `collections.deque` iterator (`dequeiter_next_lock_held`) has the same shape: it takes the deque's lock for the block walk but its own counter/index is not serialized against a concurrent `length_hint`.

## Reproducer

```python
import operator
import threading


class Seq:
    def __getitem__(self, i):
        if i >= 4096:
            raise IndexError
        return i


NTHREADS = 8
barrier = threading.Barrier(NTHREADS)


def worker(it):
    barrier.wait()
    for _ in range(8000):
        try:
            next(it)  # iter_iternext: it->it_index++
        except (StopIteration, IndexError):
            pass
        operator.length_hint(it, 0)  # iter_len: reads it->it_index


for _ in range(300):
    shared = iter(Seq())  # ONE shared seqiterobject
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
```

Run under a free-threaded TSan build (`PYTHON_GIL=0`, `TSAN_OPTIONS=…exitcode=66…`): exit **66**, deterministically, on both `debug-ft-nojit-tsan` and `release-ft-nojit-tsan`. `SUMMARY` names `iter_iternext` (write `it_index`) vs `iter_len` / `iter_iternext` (read). (Full report in `tsan_report.txt`.)

## Root cause

A builtin sequence-iterator cursor mutated with no per-object critical section and no atomics — safe under the GIL, a data race when the iterator is shared. Same structural defect as the bytes/str/struct/set/dict iterators.

## Impact / severity

**Low — value-benign, does not crash.** Unlike the bytes/str iterators (which index `ob_sval` directly and can read out of bounds), `iter_iternext` goes through `PySequence_GetItem`, which does its own bounds check — so a torn/stale `it_index` yields a **duplicate or skipped value or `StopIteration`**, never an out-of-bounds access. Per CPython's iterator free-threading strategy ([gh-124397](https://github.com/python/cpython/issues/124397), Raymond Hettinger) — *"concurrent access is allowed to return duplicate values, skip values, or raise an exception"* — this is **explicitly acceptable behavior**, which is why gh-120496 was closed.

## Suggested fix

**None intended.** This class is deemed acceptable per gh-124397; a user sharing an iterator across threads should add their own lock. (If ever hardened, `it_index` would need an atomic load/store or a per-iterator critical section — the same treatment the *memory-unsafe* members of the family, bytes/str, genuinely require for their OOB / double-DECREF faces.)

## Notes

- **This is gh-120496** ("Sequence iterator thread-safety", **CLOSED**), whose own reproducer tested `iter(range)`, list/dict iterators, and "a custom class implementing `__getitem__`" — the last of which yields exactly this `seqiterobject`. Not fileable; cataloged here so fleets dedupe it.
- **Notable only as a demonstration of `--tsan-no-halt`'s value:** this race was present in every prior fleet but was masked under `halt_on_error=1` by the dominant bytes/str/count races; it only surfaced once fleet-11 captured multiple races per session.
- Belongs to the builtin/stdlib sequence-iterator cursor family: memory-unsafe members (bytes TSAN-0037, str TSAN-0038) are fileable; value-benign members (this seqiter/deque, set TSAN-0040) are gh-120496/gh-124397-acceptable.

---

*gh-120496 (closed, acceptable per gh-124397). Recorded for the catalog; not fileable.*
