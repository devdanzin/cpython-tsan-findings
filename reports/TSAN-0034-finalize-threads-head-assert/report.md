# Data race: `handle_thread_shutdown_exception` reads `interp->threads.head` in an `assert()` *before* `_PyEval_StopTheWorld` (`pylifecycle.c:3830` vs `pystate.c:1936`)

*During interpreter finalization, `handle_thread_shutdown_exception()` asserts on the interpreter's thread-list head **before** it stops the world, reading `interp->threads.head` with no lock. A thread exiting concurrently writes that field in `tstate_delete_common()` while holding `HEAD_LOCK`. **Debug-build only** — the racing read lives inside the `assert`, so `NDEBUG` removes it.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## The race

```
Read  (main thread)  handle_thread_shutdown_exception  Python/pylifecycle.c:3830
                      <- wait_for_thread_shutdown  :3863
                      <- make_pre_finalization_calls / _Py_Finalize / Py_FinalizeEx
Write (thread T3)    tstate_delete_common             Python/pystate.c:1936
                      <- _PyThreadState_DeleteCurrent :2028  <- thread_run
Location: global '_PyRuntime'
```

## Root cause

`Python/pylifecycle.c`:

```c
static void
handle_thread_shutdown_exception(PyThreadState *tstate)
{
    assert(tstate != NULL);
    assert(_PyErr_Occurred(tstate));
    PyInterpreterState *interp = tstate->interp;
    assert(interp->threads.head != NULL);   // 3830: UNLOCKED read
    _PyEval_StopTheWorld(interp);           // 3831: world stopped only here

    // We don't have to worry about locking this because the
    // world is stopped.
    _Py_FOR_EACH_TSTATE_UNLOCKED(interp, tstate) {
        ...
    }
    _PyEval_StartTheWorld(interp);
    PyErr_FormatUnraisable("Exception ignored on threading shutdown");
}
```

The comment is accurate for the loop, but the `assert` on the line **before** `_PyEval_StopTheWorld` is outside the protection it describes. Meanwhile the writer is properly locked — `Python/pystate.c`:

```c
    HEAD_LOCK(runtime);
    if (tstate->prev) {
        tstate->prev->next = tstate->next;
    }
    else {
        interp->threads.head = tstate->next;   // 1936: WRITE under HEAD_LOCK
    }
```

So it is the classic shape: a lock-free reader against a lock-holding writer. It is reached when `threading._shutdown()` raises during `Py_FinalizeEx`, while another thread happens to be deleting its own tstate.

## Severity — debug-build only

The racing read exists **only inside `assert()`**, which `NDEBUG` compiles out, so a release build never performs it and cannot hit this race. The worst case on a debug build is a bogus assertion outcome. This is *not* in the class of TSAN-0033 (memory-safety, reproduces and wedges/crashes on release).

## Reproducer

`repro.py` (stdlib only) reproduces this in isolation — **~44 % per single run (11/25)**, so a short
loop makes it reliable:

```python
import sys, _thread, threading, time
assert not sys._is_gil_enabled(), "need a --disable-gil build with PYTHON_GIL=0"

# Make threading._shutdown() raise so wait_for_thread_shutdown() calls the handler.
def _boom():
    raise RuntimeError("forced shutdown exception")
threading._shutdown = _boom

# Keep OTHER threads continuously creating/destroying thread-states through finalization,
# so a HEAD_LOCK write of interp->threads.head is in TSan's history when the handler reads
# it unlocked. (Cheap _thread churn; avoids the threading module's own _shutdown bookkeeping.)
_running = True
def churn():
    while _running:
        try:
            _thread.start_new_thread(lambda: None, ())
        except RuntimeError:
            time.sleep(0)

for _ in range(6):
    _thread.start_new_thread(churn, ())
time.sleep(0.1)  # let the churn saturate
# fall off the end -> Py_FinalizeEx -> wait_for_thread_shutdown -> _boom() -> handler reads threads.head
```

The key over the first attempt (which created many threads *once at startup* — all dead well before
finalization, 0/14): the churn writes `interp->threads.head` **continuously, right through
finalization**, so the handler's one-shot read almost always overlaps an unsynchronized write.

TSan reports whichever writer it catches. The reproducer hits the **create** side —
`add_threadstate` (`pystate.c:1661`), via `_PyThreadState_New` ← `ThreadHandle_start` — while the
original fleet vehicle hit the **delete** side, `tstate_delete_common` (`pystate.c:1936`). Both write
`interp->threads.head` under `HEAD_LOCK` and race the same unlocked read at `pylifecycle.c:3830`; the
raw block in `tsan_report.txt` is the create-side face.

## Suggested fix

Move the assert below `_PyEval_StopTheWorld(interp)` so it is covered by the same stop-the-world the following loop already relies on — which is what the function's own comment assumes. Dropping it entirely would also do.

## Issue search

No filing found: `handle_thread_shutdown_exception` returns no hits. Neighbours are different finalization bugs — #144616 (`Py_FinalizeEx` / `_PyObject_Dump`, lost `sys.stderr`), #122517 (`Py_EndInterpreter` deadlock), #113148 (crash due to an exception in `threading._shutdown()` — most likely the issue this handler was *added* to fix), #117657 (make TSan tests pass with the GIL disabled). **Verdict: new, but low value on its own.**

## Confirmed build

`debug-ft-nojit-tsan` — CPython **3.16.0a0** free-threading (`--disable-gil --with-thread-sanitizer`), `heads/main:bcf98ddbc40`. Still unfixed there.
