# Data race: `repr()` of a shared `RLock` reads the owner thread-id non-atomically while another thread acquires/releases it (`_threadmodule.c:1291`)

*`rlock_repr` reads `self->lock.thread` (the recursive mutex's owner thread-id) with a plain C load, but `acquire()`/`release()` store that same field with `_Py_atomic_store_ullong_relaxed` (`lock.c:439`/`466`). A thread calling `repr(rlock)` on a shared `RLock` races with another thread acquiring or releasing it. `repr()` is a read-only-looking operation on a type whose entire purpose is to be shared across threads, so this is a genuine user-facing FT data race — not the fuzzer's own thread plumbing.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`_thread.RLock` (which is what `threading.RLock()` returns) wraps a `_PyRecursiveMutex`:

```c
// Include/internal/pycore_lock.h:159
typedef struct {
    PyMutex mutex;
    unsigned long long thread;  // owner: PyThread_get_thread_ident_ex()
    size_t level;               // recursion count
} _PyRecursiveMutex;
```

The owner field `thread` is **written atomically** on every acquire and release, but **read non-atomically** by `repr()`:

```c
// Modules/_threadmodule.c:1287
static PyObject *
rlock_repr(PyObject *op)
{
    rlockobject *self = rlockobject_CAST(op);
    PyThread_ident_t owner = self->lock.thread;   /* :1291  PLAIN read of m->thread */
    int locked = rlock_locked_impl(self);
    size_t count;
    if (locked) {
        count = self->lock.level + 1;             /* :1295  PLAIN read of m->level  */
    }
    ...
    return PyUnicode_FromFormat(
        "<%s %s object owner=%... count=%zu at %p>", ...);
}
```

```c
// Python/lock.c:429  (acquire path, reached from _thread_RLock_acquire_impl)
PyLockStatus
_PyRecursiveMutex_LockTimed(_PyRecursiveMutex *m, PyTime_t timeout, _PyLockFlags flags)
{
    ...
    PyLockStatus s = _PyMutex_LockTimed(&m->mutex, timeout, flags);
    if (s == PY_LOCK_ACQUIRED) {
        _Py_atomic_store_ullong_relaxed(&m->thread, thread);   /* :439  ATOMIC write */
        assert(m->level == 0);
    }
    return s;
}

// Python/lock.c:454  (release path)
int
_PyRecursiveMutex_TryUnlock(_PyRecursiveMutex *m)
{
    ...
    _Py_atomic_store_ullong_relaxed(&m->thread, 0);            /* :466  ATOMIC write */
    PyMutex_Unlock(&m->mutex);
    return 0;
}
```

Two threads — one `repr()`-ing a shared `RLock`, one `acquire()`/`release()`-ing it — race on `m->thread` (plain read vs relaxed-atomic write). It is value-benign (a single aligned 8-byte word; `repr()` may just print a stale owner id / count), but it is a genuine TSan-reported data race, and strictly it is UB in C: a plain read concurrent with an atomic write.

## Reproducer

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NA = 3          # acquirer threads (store m->thread on acquire/release)
NR = 3          # reprer threads   (plain read of m->thread)
NLOCKS = 32
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NA + NR + 1)
leave = threading.Barrier(NA + NR + 1)

def acquirer():
    for _ in range(ROUNDS):
        enter.wait()
        for lk in pool[0]:
            lk.acquire()        # atomic store m->thread = tid
            lk.release()        # atomic store m->thread = 0
        leave.wait()

def reprer():
    for _ in range(ROUNDS):
        enter.wait()
        for lk in pool[0]:
            repr(lk)            # rlock_repr: plain read of self->lock.thread (:1291)
        leave.wait()

ts = [threading.Thread(target=acquirer) for _ in range(NA)]
ts += [threading.Thread(target=reprer) for _ in range(NR)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [threading.RLock() for _ in range(NLOCKS)]  # fresh shared locks each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, glibc, ASLR off)

```
WARNING: ThreadSanitizer: data race
  Atomic write of size 8 by thread T2:
    #0 _Py_atomic_store_ullong_relaxed  Include/cpython/pyatomic_gcc.h:518:3
    #1 _PyRecursiveMutex_LockTimed      Python/lock.c:439:9
    #2 _thread_RLock_acquire_impl       Modules/_threadmodule.c:1102:22    (lk.acquire())
    ...
    #28 thread_run                      Modules/_threadmodule.c:388:21

  Previous read of size 8 by thread T4:
    #0 rlock_repr                       Modules/_threadmodule.c:1291:41    (self->lock.thread)
    #1 PyObject_Repr                    Objects/object.c:784:11
    #2 builtin_repr                     Python/bltinmodule.c:2677:12       (repr(lk))
    ...

SUMMARY: ThreadSanitizer: data race Modules/_threadmodule.c:1291:41 in rlock_repr
```

Reproduces deterministically and quickly (exit 66, ~0.6 s, 3/3 runs). The two racing functions and the flagged line (`_threadmodule.c:1291:41 in rlock_repr` vs the relaxed-atomic store in `_PyRecursiveMutex_LockTimed`) match the fuzzer-seeded signature exactly; the seed vehicle reached `rlock_repr` via `"%s" % lock` (`object_str` → `slot_tp_repr`), the minimal repro reaches it via `repr(lock)` (`PyObject_Repr`) — same slot, same field, same race.

## Root cause

The owner field `_PyRecursiveMutex.thread` is deliberately accessed atomically on the mutating side — `_PyRecursiveMutex_LockTimed`/`_Lock` store it with `_Py_atomic_store_ullong_relaxed` (`lock.c:439`, `:425`) and `_PyRecursiveMutex_TryUnlock` clears it the same way (`lock.c:466`) — precisely because it is read/written across threads. But `rlock_repr` (`_threadmodule.c:1291`) reads the same field with a plain C load, and it holds no lock while doing so (`repr()` is intentionally non-blocking so it can describe a lock another thread owns). The atomic-write / plain-read pair is a data race.

The recursion count `self->lock.level` (read plainly at `:1295`) is a secondary racy field: it is mutated (`m->level++`/`--`) only by the owning thread while it holds the mutex, but `rlock_repr` reads it without the mutex, so a concurrent `repr()` during a recursive (re)acquire also races. TSan halted on the `thread` read first (`halt_on_error=1`), so `thread` is the confirmed signature; `level` should be fixed in the same change.

## Impact / severity

**Low.** Value-benign and crash-free: the racing datum is a single naturally-aligned 8-byte word, so on the supported platforms the load is not torn — `repr()` merely risks printing a momentarily stale `owner=`/`count=` (e.g. an owner id that was just cleared to 0, or vice-versa). No memory-safety consequence. But it is a real, TSan-reported data race on `repr()` of a **public, explicitly-shareable** synchronization primitive, and strictly UB under the C memory model (plain read concurrent with atomic write). Same class and severity as TSAN-0005 (`dec_hash`): a read-only-looking C accessor reading a field that is written atomically elsewhere.

## Real bug vs. framework noise (the `tsanFRAME` call)

**Real, user-facing bug — the `tsanFRAME` label is a misclassification.** The fleet deduper labels this vehicle `tsanFRAME` because the racing frames live in `Modules/_threadmodule.c`, which it treats as thread-machinery scaffolding. That heuristic is wrong here:

- `rlock_repr` is the `tp_repr` slot of `_thread.RLock` — a **public type** (`threading.RLock()` returns exactly this object). `repr(some_rlock)` is ordinary user code (logging, debugging, `%r`/`%s` formatting, `reprlib`).
- `RLock`'s *entire reason to exist* is to be shared and acquired/released across threads. A second thread calling `repr()` on it while it is being (re)acquired is a completely normal, sanctioned usage pattern.
- The race is on the **shared RLock object's own metadata** (`m->thread`), not on any fuzzer-private data structure. That the reader/writer threads in the vehicle happen to be the harness's threads is irrelevant — a real program with a shared lock and a logging thread hits the identical access pair.
- The mutating side already uses relaxed atomics, showing the developers know this field is concurrently accessed; the `repr()` reader simply was not updated to match. That asymmetry is the bug.

## Suggested fix

Make the reads in `rlock_repr` atomic to match the writers (relaxed ordering is sufficient — the value is a single word and only informational here):

```c
static PyObject *
rlock_repr(PyObject *op)
{
    rlockobject *self = rlockobject_CAST(op);
    PyThread_ident_t owner = _Py_atomic_load_ullong_relaxed(&self->lock.thread);
    int locked = rlock_locked_impl(self);   // already atomic via PyMutex_IsLocked
    size_t count = locked
        ? _Py_atomic_load_size_relaxed(&self->lock.level) + 1
        : 0;
    return PyUnicode_FromFormat(
        "<%s %s object owner=%" PY_FORMAT_THREAD_IDENT_T " count=%zu at %p>",
        locked ? "locked" : "unlocked", Py_TYPE(self)->tp_name, owner, count, self);
}
```

(`FT_ATOMIC_*` / `_Py_atomic_load_*_relaxed` wrappers exist for exactly this pattern in the free-threaded build.) The mirror accessor `_thread_RLock__recursion_count_impl` (`:1250`) reads `self->lock.level` under `_PyRecursiveMutex_IsLockedByCurrentThread`, i.e. only when the current thread owns the lock, so it is not part of this race.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`). Low severity (value-benign, crash-free), but a real data race on a read-only-looking public API. Mirrors TSAN-0005 (`dec_hash`) structurally: a plain read of a field written atomically elsewhere. The general fix pattern — audit read-only C accessors (`tp_repr`, `tp_str`, introspection helpers) that touch fields mutated atomically under free-threading — applies across the interpreter. `Modules/_threadmodule.c` should be checked for other plain reads of `_PyRecursiveMutex`/`PyMutex` internals in non-locking accessors.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
