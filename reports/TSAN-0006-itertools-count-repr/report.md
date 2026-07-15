# Data race: `itertools.count().__repr__` reads the counter non-atomically while `__next__` advances it atomically (`itertoolsmodule.c:3612`)

*In the free-threaded build, `count_next` advances a fast-mode `count()`'s counter `lz->cnt` with an **atomic** compare-exchange (`itertoolsmodule.c:3599`), but `count_repr` reads that same field with a **plain, non-atomic** load (`itertoolsmodule.c:3612`). Concurrently `repr()`-ing a shared `count()` while another thread calls `next()` on it is a data race: an atomic store racing a non-atomic load on `lz->cnt`. `repr()` looks read-only to callers, so a shared `count()` is not safe to display while it advances.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`itertools.count()` keeps its fast-mode counter in the plain C field `countobject.cnt` (`Py_ssize_t`). The free-threaded `count_next` was hardened to advance it with a lock-free CAS loop over relaxed atomics:

```c
static PyObject *
count_next(PyObject *op)
{
    countobject *lz = countobject_CAST(op);
#else /* Py_GIL_DISABLED */
    Py_ssize_t cnt;
    cnt = _Py_atomic_load_ssize_relaxed(&lz->cnt);        /* :3591 atomic load  */
    for (;;) {
        if (cnt == PY_SSIZE_T_MAX) { ... }                /* slow-mode fallback */
        if (_Py_atomic_compare_exchange_ssize(&lz->cnt, &cnt, cnt + 1))  /* :3599 atomic write */
            return PyLong_FromSsize_t(cnt);
    }
#endif
}
```

But `count_repr` was **not** updated to match -- it reads the same field with an ordinary C load:

```c
static PyObject *
count_repr(PyObject *op)
{
    countobject *lz = countobject_CAST(op);
    if (lz->long_cnt == NULL)
        return PyUnicode_FromFormat("%s(%zd)",
                                    _PyType_Name(Py_TYPE(lz)), lz->cnt);  /* :3612 plain read */
    ...
}
```

Two threads sharing one `count()` -- one calling `repr(c)`, another calling `next(c)` -- race on `lz->cnt`: `count_repr`'s plain 8-byte load at `:3612:68` vs `count_next`'s atomic 8-byte store at `:3599`. Under C11/TSan semantics a non-atomic access concurrent with an atomic access to the same location is a data race (and technically UB). It is value-benign in practice (aligned 8-byte word, no torn read on x86-64; `repr` just prints a slightly stale count), but it is a genuine TSan-reported race on an operation callers treat as read-only.

## Reproducer

```python
import sys, threading, itertools
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A fast-mode itertools.count() keeps its counter in the plain C field lz->cnt.
# count_next() advances it with an ATOMIC compare-exchange, but count_repr()
# reads the very same field with a PLAIN (non-atomic) load. Sharing one count()
# across threads -- some calling next(c), some calling repr(c) -- races the plain
# read in count_repr against the atomic write in count_next on lz->cnt.
NT = 6                    # worker threads (half repr, half next)
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def repr_worker():
    for _ in range(ROUNDS):
        enter.wait()
        for c in pool[0]:
            repr(c)          # count_repr: plain read of lz->cnt (itertoolsmodule.c:3612)
        leave.wait()

def next_worker():
    for _ in range(ROUNDS):
        enter.wait()
        for c in pool[0]:
            next(c)          # count_next: atomic CAS write of lz->cnt (itertoolsmodule.c:3599)
        leave.wait()

ts = [threading.Thread(target=repr_worker) for _ in range(NT // 2)]
ts += [threading.Thread(target=next_worker) for _ in range(NT - NT // 2)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [itertools.count() for _ in range(64)]   # fresh, fast-mode counts each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, debug-ft-nojit-tsan)

```
WARNING: ThreadSanitizer: data race
  Atomic write of size 8 at 0x... by thread T4:
    #0 _Py_atomic_compare_exchange_ssize Include/cpython/pyatomic_gcc.h:130:10
    #1 count_next   Modules/itertoolsmodule.c:3599:13   (CAS lz->cnt = cnt + 1)
    #2 builtin_next Python/bltinmodule.c:1776:11
    ...
    #27 thread_run  Modules/_threadmodule.c:388:21

  Previous read of size 8 at 0x... by thread T2:
    #0 count_repr    Modules/itertoolsmodule.c:3612:68   (read lz->cnt)
    #1 PyObject_Repr Objects/object.c:784:11
    #2 builtin_repr  Python/bltinmodule.c:2677:12
    ...
    #30 thread_run   Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/itertoolsmodule.c:3612:68 in count_repr
```

Reproduces deterministically (exit 66) and does not crash. The racing pair is always `count_repr` (plain read of `lz->cnt`, `:3612:68`) vs `count_next` (atomic write of `lz->cnt`, `:3599`). TSan's `SUMMARY` names whichever side it reports as the "current" access, so it alternates between `count_repr:3612` (the seeded signature) and `_Py_atomic_compare_exchange_ssize` / `count_next:3599` across runs -- same race either way. This is the exact signature auto-seeded by `fusil --tsan` in fleet 01 (the vehicle's op-mix ran `list(c)` -- which drives `count_next` the same way -- concurrently with `repr(c)`).

## Root cause

The free-threading conversion of `count` made the *writer* (`count_next`) atomic on `lz->cnt` -- a relaxed atomic load at `:3591` plus a `_Py_atomic_compare_exchange_ssize` at `:3599` -- but left the *reader* in `count_repr` as a plain field access (`:3612`). C11's memory model (and TSan) require every access to a location that is atomically written to also be atomic; a plain load concurrent with an atomic store is a data race. The object presents `repr()` as read-only, so callers reasonably assume it is safe to display a shared `count()` while another thread advances it -- but the hidden plain read of `cnt` breaks that.

(The `count()` object is otherwise partly hardened for free-threading: the fast->slow transition and `count_nextlong` in `count_next` run under `Py_BEGIN_CRITICAL_SECTION(lz)`. Only `count_repr`'s fast-mode `cnt` read was missed. In fast mode `lz->long_cnt` stays `NULL`, so `count_repr`'s `:3610` `long_cnt == NULL` branch is stable -- the sole reported race is the `cnt` field.)

## Impact / severity

Low. Value-benign and crash-free: `lz->cnt` is a single aligned `Py_ssize_t`, so there is no torn read on mainstream 64-bit platforms; the worst observable effect is `repr()` printing a slightly stale counter value. But it is a real, TSan-reported data race (technically UB) on an API callers treat as read-only, and it is trivially triggered by sharing a `count()` across threads. No use-after-free or memory-safety consequence.

## Suggested fix

Read `lz->cnt` in `count_repr` with the same relaxed atomic that `count_next` already uses on the field, so the read/write pair is well-defined and TSan-clean (the value is a plain counter, so relaxed ordering suffices):

```c
static PyObject *
count_repr(PyObject *op)
{
    countobject *lz = countobject_CAST(op);
    if (lz->long_cnt == NULL)
        return PyUnicode_FromFormat("%s(%zd)", _PyType_Name(Py_TYPE(lz)),
                                    _Py_atomic_load_ssize_relaxed(&lz->cnt));  /* was: lz->cnt */
    ...
}
```

(`FT_ATOMIC_LOAD_SSIZE_RELAXED` / `_Py_atomic_load_ssize_relaxed` is the existing wrapper for exactly this; `count_next` already loads the field this way at `:3591`.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 01). Low severity (value-benign, crash-free), but a real free-threading data race left by an incomplete atomic conversion -- the writer was made atomic and the reader was not. The same "atomic write, plain read" asymmetry should be audited across the other partially-hardened itertools objects (any field where `_Py_atomic_*` is used on one path but read plainly on another).

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
