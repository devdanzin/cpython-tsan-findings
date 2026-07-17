# Data race: `handle_thread_shutdown_exception` reads `interp->threads.head` in an `assert()` *before* `_PyEval_StopTheWorld` (`pylifecycle.c:3830` vs `pystate.c:1936`)

*During interpreter finalization, `handle_thread_shutdown_exception()` asserts on the interpreter's thread-list head **before** it stops the world, reading `interp->threads.head` with no lock. A thread exiting concurrently writes that field in `tstate_delete_common()` while holding `HEAD_LOCK`. **Debug-build only** — the racing read lives inside the `assert`, so `NDEBUG` removes it.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

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

## Reproduction status

**Not reproduced in isolation** — observed once in `fusil-tsan_fleet_04` (raw report in `tsan_report.txt`). A targeted attempt (override `threading._shutdown` so it raises — which does reach the handler, confirmed by its `PyErr_FormatUnraisable` output — while 400 no-op threads exit concurrently) did not trip TSan in 14 runs. The window is a single unlocked pointer read against a locked write. The root cause is nonetheless unambiguous from the source above.

## Suggested fix

Move the assert below `_PyEval_StopTheWorld(interp)` so it is covered by the same stop-the-world the following loop already relies on — which is what the function's own comment assumes. Dropping it entirely would also do.

## Issue search

No filing found: `handle_thread_shutdown_exception` returns no hits. Neighbours are different finalization bugs — #144616 (`Py_FinalizeEx` / `_PyObject_Dump`, lost `sys.stderr`), #122517 (`Py_EndInterpreter` deadlock), #113148 (crash due to an exception in `threading._shutdown()` — most likely the issue this handler was *added* to fix), #117657 (make TSan tests pass with the GIL disabled). **Verdict: new, but low value on its own.**

## Confirmed build

`debug-ft-nojit-tsan` — CPython **3.16.0a0** free-threading (`--disable-gil --with-thread-sanitizer`), `heads/main:bcf98ddbc40`. Still unfixed there.
