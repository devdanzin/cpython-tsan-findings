# Data race: a shared `set` iterator's private cursor (`si->len`, `si->si_pos`) is advanced non-atomically (`setiter_iternext` vs `setiter_len`, `Objects/setobject.c`)

*The set iterator (`setiterobject`, from `iter(set)`/`iter(frozenset)`) keeps its position in plain fields — `len` (remaining-count), `si_pos` (table index), `si_used` (size snapshot). `setiter_iternext` advances them with `si->len--` and `si->si_pos = i+1` **outside** any critical section (the `Py_BEGIN_CRITICAL_SECTION` it holds is on the **set** `so`, not the iterator), while `setiter_len` (the `__length_hint__` slot) plainly **reads** `si->len` and `si->si_used`. Sharing one set iterator across threads is a data race on its private cursor — the `set` sibling of the bytes/str/struct iterator races.*

**Prior art:** [gh-112069](https://github.com/python/cpython/issues/112069) ("Make `set` thread-safe in `--disable-gil` builds", CLOSED) + [PR #117935](https://github.com/python/cpython/pull/117935) ("Make `setiter_iternext` to be thread-safe", MERGED) made concurrent iteration of a shared *set* safe, but did **not** protect the *iterator's own* cursor — so a shared *iterator* still races. Same class as the filed [str #153928](https://github.com/python/cpython/issues/153928) / [struct #154013](https://github.com/python/cpython/issues/154013) iterator races.

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`setiterobject` (`Objects/setobject.c:1033`) is:

```c
typedef struct {
    PyObject_HEAD
    PySetObject *si_set; /* Set to NULL when iterator is exhausted */
    Py_ssize_t si_used;
    Py_ssize_t si_pos;
    Py_ssize_t len;
} setiterobject;
```

`setiter_iternext` (partially hardened by PR #117935) atomic-loads `si->si_used` and takes a critical section **on the set** for the table walk, but advances the **iterator's own** fields with plain writes *outside* that section:

```c
Py_ssize_t si_used = FT_ATOMIC_LOAD_SSIZE_RELAXED(si->si_used);   /* atomic read */
if (si_used != so_used) { ... si->si_used = -1; ... }             /* PLAIN write */
Py_BEGIN_CRITICAL_SECTION(so);        /* section is on the SET, not the iterator */
    i = si->si_pos;
    ... table scan ...
Py_END_CRITICAL_SECTION();
si->si_pos = i+1;                     /* PLAIN write, OUTSIDE the section */
...
si->len--;                            /* PLAIN write, OUTSIDE the section */
```

`setiter_len` (`__length_hint__`, untouched by #117935) plainly reads the same fields:

```c
setiter_len(PyObject *op, ...)
{
    setiterobject *si = (setiterobject*)op;
    Py_ssize_t len = 0;
    if (si->si_set != NULL && si->si_used == si->si_set->used)   /* PLAIN read si->si_used */
        len = si->len;                                           /* PLAIN read si->len */
    return PyLong_FromSsize_t(len);
}
```

Two threads sharing one iterator — one calling `next()` (writes `si->len`/`si->si_pos`), another calling `operator.length_hint()` (reads `si->len`) — race on the `si->len` word. TSan reports `setiter_iternext` vs `setiter_len` (and `setiter_iternext` self-race when two threads both `next()`).

## Reproducer

```python
import operator
import threading

# A shared set iterator: some threads advance it (setiter_iternext) while others read its
# cursor via operator.length_hint (setiter_len) -> data race on the non-atomic countdown/index.
NTHREADS = 8
ITERS = 20000
barrier = threading.Barrier(NTHREADS)


def worker(it):
    barrier.wait()
    for _ in range(ITERS):
        try:
            next(it)  # setiter_iternext: advance the shared cursor
        except StopIteration:
            pass
        operator.length_hint(it, 0)  # setiter_len: read the shared cursor


for _ in range(300):
    shared = iter(set(range(4096)))  # ONE shared set iterator
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
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
  Read of size 8 by thread T5:
    #0 setiter_len                 Objects/setobject.c:1063   (len = si->len)
    #1 cfunction_vectorcall_NOARGS Objects/methodobject.c:508
    ...                            (operator.length_hint(it, 0))

  Previous write of size 8 by thread T?:
    #0 setiter_iternext            Objects/setobject.c        (si->len--)
    ...                            (next(it))
SUMMARY: ThreadSanitizer: data race Objects/setobject.c:1063 in setiter_len
```

Fleet-10 drove the `setiter_iternext | setiter_len` face (6 vehicles, the fleet's most common new signature); the isolated repro also drives the `setiter_iternext | setiter_iternext` self-race. (Full report in `tsan_report.txt`.)

## Root cause

PR #117935 (gh-112069) made **concurrent iteration of a shared set** safe: it converted `si->si_used`/`so->used` reads to atomics and wrapped the table scan in `Py_BEGIN_CRITICAL_SECTION(so)`. That critical section is keyed on the **set object**, so it serializes threads that each hold their **own** iterator over the same set. It does **not** serialize two threads sharing **one** iterator, because the iterator's private cursor — `si->len`, `si->si_pos`, and the `si->si_used = -1` sticky-exhaustion write — is mutated with plain writes *outside* the section, and `setiter_len` reads it with no section at all. So when the *iterator itself* is shared, its cursor fields race. This is the same shared-iterator-cursor defect already accepted for the str (#153928) and struct (#154013) iterators — the set iterator is the sibling that #117935's set-keyed fix did not reach.

## Impact / severity

**Low–moderate.** A data race on the iterator's countdown/index: under concurrency it yields a stale/mis-stepped `__length_hint__`, or duplicated/skipped elements, or a spuriously-tripped "Set changed size during iteration". Sharing one iterator across threads is unusual (which caps real-world priority), but it is a genuine C11 data race in exactly the primitive that #112069/#117935 set out to make thread-safe. Free-threaded build only.

## Suggested fix

Protect the iterator's *own* cursor, mirroring what the str/bytes/struct iterators need:

- take a per-**iterator** critical section (`Py_BEGIN_CRITICAL_SECTION(si)`) over the read-modify-write of `si->len`/`si->si_pos`/`si->si_used` in `setiter_iternext`, and the read in `setiter_len`; **or**
- make those three fields atomic (relaxed loads/stores), as `so->used` and `si->si_used`'s load already are.

The whole builtin sequence/collection-iterator family shares this shape; a uniform "shared iterator cursor" policy would cover set, str, bytes, struct, and dict at once.

## Notes

- **Not covered by the merged fix.** gh-112069/#117935 hardened `setiter_iternext`'s access to the *set*; the per-*iterator* cursor was left plain. Fileable as a follow-up to gh-112069 (same shared-iterator class as the filed #153928 / #154013), or folded into the builtin-iterator family umbrella #153852. Outward-facing — awaiting maintainer go-ahead.
- Related: gh-120496 ("Sequence iterator thread-safety") is the general shared-iterator question for the list/tuple sequence iterators; this is the `set` collection iterator.
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 10): the op-mix shared-iterator path shares one `iter(set(...))` across workers, some advancing it with `next()` and some reading its cursor with `operator.length_hint`.

---

*Part of the builtin-iterator shared-cursor family (TSAN-0037 bytes / TSAN-0038 str / TSAN-0039 struct / TSAN-0026 dict). Not yet individually filed.*
