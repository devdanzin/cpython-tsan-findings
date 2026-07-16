# Premature free / refcount underflow: `TaskObj_dealloc` leaves an `_asyncio.Task` refcount-0 **while still GC-tracked** across a `_PyEval_StopTheWorld`, aborting a concurrent `gc.collect()` (`_asynciomodule.c:2963` vs `gc_free_threading.c:1083`)

*On the free-threaded build, `TaskObj_dealloc` runs the Task finalizer and then `unregister_task()` — which for a task deallocated on a thread other than its creator performs a full `_PyEval_StopTheWorld()` — **before** it calls `PyObject_GC_UnTrack(self)`. During that window the Task is at refcount 0 yet still on the GC's tracked list. A `gc.collect()` on another thread stops the world at a safe point inside that window, and its Py_DEBUG invariant check `validate_refcounts()` finds the refcount-0 tracked Task and aborts. This is a use-after-free-class memory-safety bug, not a TSan data-race stanza.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

The crash is a fatal Py_DEBUG assertion, killing the process with `SIGABRT` (exit 134):

```
Python/gc_free_threading.c:1083: validate_refcounts: Assertion
  "_Py_REFCNT(((PyObject*)((op)))) > 0" failed:
  tracked objects must have a reference count > 0
object type name: _asyncio.Task
object refcount : 0
Fatal Python error: _PyObject_AssertFailed
```

`validate_refcounts()` runs at the start of a free-threaded `gc.collect()` under stop-the-world and asserts that *every GC-tracked object has refcount > 0*. Finding a tracked `_asyncio.Task` at refcount 0 means the Task has been logically freed (its last reference dropped) but is still linked into the GC tracked set — a premature-free / dangling-tracked-object condition.

The fuzzer vehicle hammered a shared `_asyncio` module + `_asyncio.Future` from 4 threads doing concurrent method calls, attribute churn, and `gc.collect()`; the victim is a **transient `_asyncio.Task`** created (and torn down) during that churn. 58 identical vehicles were observed, all `_asyncio.Task`, all this exact assertion.

## Root cause

`Modules/_asynciomodule.c`, `TaskObj_dealloc` (lines 2963–2981):

```c
static void
TaskObj_dealloc(PyObject *self)
{
    if (PyObject_CallFinalizerFromDealloc(self) < 0) {
        return; // resurrected
    }
    // unregister the task after finalization so that
    // if the task gets resurrected, it remains registered
    unregister_task((TaskObj *)self);   // 2971  <-- may _PyEval_StopTheWorld

    PyTypeObject *tp = Py_TYPE(self);
    PyObject_GC_UnTrack(self);           // 2974  <-- untrack happens ONLY here
    PyObject_ClearWeakRefs(self);
    (void)TaskObj_clear(self);
    tp->tp_free(self);
    Py_DECREF(tp);
}
```

By the time `TaskObj_dealloc` runs, the Task's refcount is already 0. It stays **refcount-0-and-still-GC-tracked** across two operations that can hit a safe point / stop the world before the `PyObject_GC_UnTrack` at line 2974:

1. `PyObject_CallFinalizerFromDealloc` → `TaskObj_finalize` (runs Python-visible `__del__` / `call_exception_handler`).
2. `unregister_task((TaskObj *)self)` (lines 2200–2220):

```c
static void
unregister_task(TaskObj *task)
{
#ifdef Py_GIL_DISABLED
    if (task->task_tid == _Py_ThreadId()) {
        unregister_task_safe(task);          // same thread: cheap, no STW
    } else {
        PyThreadState *tstate = _PyThreadState_GET();
        _PyEval_StopTheWorld(tstate->interp);   // <-- cross-thread branch
        unregister_task_safe(task);
        _PyEval_StartTheWorld(tstate->interp);
    }
#else
    unregister_task_safe(task);
#endif
}
```

When another thread calls `gc.collect()`, it stops the world and runs `validate_refcounts()` over the tracked set. If it pauses the deallocating thread anywhere in the window above, it observes the refcount-0 tracked Task and aborts.

### Why a Task and not a Future (the isolating control)

`FutureObj_dealloc` (lines 1786–1799) has the *same* finalizer-before-untrack shape:

```c
FutureObj_dealloc(PyObject *self)
{
    if (PyObject_CallFinalizerFromDealloc(self) < 0) return;
    PyObject_GC_UnTrack(self);   // untrack immediately after the finalizer
    ...
}
```

but a concurrent-construct-and-drop stress of `_asyncio.Future()` + `gc.collect()` **does not** reproduce (0/8), and neither does a plain Python class whose `__init__` raises (0/8). The *only* extra work `TaskObj_dealloc` does inside the refcount-0-tracked window is `unregister_task()`, whose cross-thread branch is a `_PyEval_StopTheWorld()` — a heavyweight synchronization point that both widens the window enormously and blocks against the collector's own stop-the-world. That is what makes the Task, and only the Task, reliably caught.

### Why the malformed `Task([1,2,3])` trigger is so reliable

`_asyncio_Task___init___impl` (lines 2299–2386) assigns `self->task_tid = _Py_ThreadId()` at **line 2334**, but only *after* the coroutine check at **lines 2311–2321** rejects a non-coroutine argument. A `Task([1, 2, 3])` therefore fails `__init__` before `task_tid` is ever set, so `task_tid` keeps its zero-initialized value. In `unregister_task`, `task->task_tid (0) == _Py_ThreadId()` is always false, so **every** such transient Task takes the `_PyEval_StopTheWorld` branch on teardown — maximizing the refcount-0-tracked window on every iteration. (A *well-formed* Task deallocated on a thread other than the one that created it takes the identical branch, since `task_tid` then holds the creator's id.)

## Reproducer

`repro.py` (stdlib + `_asyncio` only). 4 threads each construct transient malformed Tasks in a tight loop and `gc.collect()` every 16th iteration, lined up on a `threading.Barrier`. It is a Py_DEBUG GC invariant, so it fires on the plain debug free-threaded build too — this is **not** a ThreadSanitizer artifact.

```python
import sys, gc, threading, _asyncio
assert not sys._is_gil_enabled()

N, ITERS = 4, 6000
barrier = threading.Barrier(N)

def worker():
    barrier.wait()
    for i in range(ITERS):
        try:
            _asyncio.Task([1, 2, 3])   # fails coro-check before task_tid is set
        except Exception:
            pass
        if i % 16 == 0:
            gc.collect()               # validate_refcounts() catches the refcount-0 tracked Task

ts = [threading.Thread(target=worker) for _ in range(N)]
for t in ts: t.start()
for t in ts: t.join()
```

### Observed reliability

- `debug-ft-nojit-tsan` under the fleet TSan wrapper (`setarch -R`, `ulimit -v unlimited`, `PYTHON_GIL=0`, `TSAN_OPTIONS=...exitcode=66`): **8/8 SIGABRT (exit 134)**, `validate_refcounts` each time, no data-race stanza (it aborts first).
- `debug-ft-nojit` (plain free-threaded debug, no TSan): **10/10 SIGABRT**.
- Two threads suffice: malformed-Task variant at `N=2` was **8/8**.
- The three original vehicles reproduce as-is: the `_asyncio` vehicle `source.py` is **8/8** under the TSan wrapper and **8/10** on the plain debug build.

## Suggested fix

Untrack the object from the GC **before** running any code that can reach a safe point (the finalizer) or stop the world (`unregister_task`) — i.e. move `PyObject_GC_UnTrack(self)` to the top of `TaskObj_dealloc`, matching the standard CPython dealloc discipline (`subtype_dealloc` untracks first). On resurrection the object is re-tracked by the resurrection path, so this is compatible with the gh-142556 fix that deliberately keeps `unregister_task` *after* finalization. An object at refcount 0 must not remain on the GC tracked list across a `_PyEval_StopTheWorld`.

A narrower alternative — untrack immediately before `unregister_task` — closes the dominant (stop-the-world) window but still runs the finalizer while tracked-at-refcount-0; untracking first is cleaner and also covers any safe point inside `TaskObj_finalize`.

## Issue search / scope

Searched `python/cpython` (issues + PRs) for `_asyncio free-threading`, `asyncio Task data race`, `asyncio Task refcount`, `TaskObj_dealloc`, `unregister_task StopTheWorld`, `validate_refcounts asyncio`, `Task dealloc UnTrack free-threading`, `asyncio Task gc tracked refcount`, and more. Closest matches:

- **gh-142556** (CLOSED, fixed by #142565 / commit `42d2bedb875`, Kumar Aditya) — "Use-after-free in asyncio Task deallocation via re-registering task in `call_exception_handler`". Same function. Its fix **created the current ordering** (finalize → `unregister_task` → untrack). Distinct bug (re-registration UAF, single-threaded); the fix left `PyObject_GC_UnTrack` last, which is precisely the window this report exploits. **Directly related.**
- **gh-128656** (CLOSED) — `test_all_tasks_race` intermittent crash on `_asyncio.Task`, but a *different* assertion (`PyObject_CallFinalizerFromDealloc: called on object with a non-zero refcount`, refcount 1). Same area (Task dealloc under FT), different invariant.
- **gh-130221 / gh-130380 / gh-145340 / gh-142556** — other Task-dealloc / asyncio-GC crashes, all CLOSED and distinct.
- **gh-116738** (OPEN, "Audit all built-in modules for thread safety") and **gh-149816** (OPEN, "22 free-threading race conditions") — this falls squarely in gh-116738's `_asyncio` FT-hardening remit; gh-149816's body does not list asyncio/Task, so this is not one of its 22.

**Verdict: NEW.** No open or closed issue describes this refcount-0-while-GC-tracked / `validate_refcounts` abort in `TaskObj_dealloc`. It is a fresh consequence of the gh-142556 fix's dealloc ordering.

Per the Yhg1s ruling (2026-07-15), concurrent use of a shared builtin object is in scope — and this is memory-safety (premature-free class), not a benign data race, so it weighs heavier. Caveat on severity: asyncio Tasks/Futures are conventionally bound to a single event loop / thread, and the most *reliable* trigger uses an intentionally malformed Task, which lowers real-world likelihood. But the identical code path is taken by any well-formed Task deallocated on a thread other than its creator (a normal free-threaded pattern: create tasks on worker threads, drop them elsewhere), and the collector that trips the assert is just an ordinary `gc.collect()`.

## Confirmed build

`python_build_matrix/builds/debug-ft-nojit-tsan` — CPython **3.16.0a0** free-threading (`--disable-gil --with-thread-sanitizer`), `heads/main:bcf98ddbc40` (2026-07-04), Clang 21. Also reproduced on the non-TSan sibling `debug-ft-nojit` at the same commit. Still unfixed on `bcf98ddbc40`.

## Release-build behaviour — this is not a Py_DEBUG-only assertion

`validate_refcounts()` is a `Py_DEBUG` GC check, so an obvious question is whether this only
matters on debug builds. It does not. Re-running the same `repro.py` on
**`release-ft-nojit`** (CPython 3.16.0a0, `--disable-gil`, `Py_DEBUG=False`, no sanitizer):

| build | N=4, ITERS=40 (160 transient Tasks) |
|-------|-------------------------------------|
| `debug-ft-nojit` | `validate_refcounts` abort, exit 134 (3/3) |
| **`release-ft-nojit`** | **wedges — 5/5 runs, never completes** |
| control (identical shape, plain raising `__init__`) | completes cleanly in **0.11 s** (5/5) |

The release wedge is a **hard livelock of the whole interpreter**, not slowness:

- One run was still wedged after **150 s** for work the control finishes in 0.11 s, burning
  **100 % CPU** the whole time (`user` ≈ `real`, i.e. spinning rather than blocked).
- It reproduces down to **N=2, ITERS=5** — ten transient Tasks — which rules out "just slow".
- A **segfault** (exit 139, core dumped) was also observed at `ITERS=150`.
- The control isolates it to the `_asyncio.Task` dealloc path: same thread count, same
  `gc.collect()` cadence, same transient refcount-0 objects, but no `unregister_task()` /
  `_PyEval_StopTheWorld()` in the dealloc — and it never wedges.

Attaching gdb to a wedged process (full stacks in **`release_hang_backtrace.txt`**) shows the
mechanism:

```
Thread t1  (spinning, 100% CPU):
  #0  _mi_page_free_collect            Objects/mimalloc/page.c:245
  #5  mi_heap_visit_blocks(visitor=update_refs)
  #6  gc_visit_heaps_lock_held         Python/gc_free_threading.c:395
  #8  deduce_unreachable_heap          Python/gc_free_threading.c:1447
  #10 gc_collect_main                  Python/gc_free_threading.c:2257   <- gc.collect()

Threads t0 / t2 / t3  (all parked):
  _PyMutex_Lock(&_PyRuntime+169592) <- _PyParkingLot_Park <- _PySemaphore_Wait
```

That is: the refcount-0-but-still-GC-tracked Task corrupts the mimalloc page state that the GC's
`update_refs` heap walk traverses, so `gc.collect()` **spins forever inside
`_mi_page_free_collect`** while every other thread blocks on the runtime mutex behind it. The
Py_DEBUG assertion is CPython catching this invariant violation *before* it turns into the
release-build wedge — exactly what such invariant checks are for.

Note that release+TSan and release+ASan runs also time out on this same wedge, so neither
sanitizer ever gets far enough to emit a report; the absence of an ASan use-after-free report is
the wedge, **not** evidence that the window is benign.

*(Reproduced on a machine with `kernel.yama.ptrace_scope=1`; the variant used for the gdb attach
adds a one-line `prctl(PR_SET_PTRACER_ANY)` so gdb can attach to a non-child process. That call
has no bearing on the bug.)*
