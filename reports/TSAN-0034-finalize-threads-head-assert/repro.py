"""TSAN-0034 repro candidate v2 — continuous thread-state churn through finalization.

The race: handle_thread_shutdown_exception() (Python/pylifecycle.c:3830) does an UNLOCKED
    assert(interp->threads.head != NULL);
one line BEFORE it stops the world (:3831). A thread that is creating or exiting concurrently
writes interp->threads.head in tstate_delete_common / the tstate-create path, holding HEAD_LOCK
(Python/pystate.c:1936). Reader unlocked vs writer locked -> data race on the global _PyRuntime.

The handler runs once, on the main thread, during Py_FinalizeEx -> wait_for_thread_shutdown(),
when threading._shutdown() raises. The earlier attempt created many threads once at startup:
they all died long before finalization, so nothing was writing threads.head when the handler
read it. This version keeps OTHER threads continuously creating and destroying thread-states
right up to and through finalization, so a HEAD_LOCK write is almost always in TSan's history
when the unlocked read happens.
"""
import sys
import _thread
import threading
import time

assert not sys._is_gil_enabled(), "need a --disable-gil build with PYTHON_GIL=0"

# Make threading._shutdown() raise so wait_for_thread_shutdown() takes the branch that calls
# handle_thread_shutdown_exception(tstate) (whose assert(_PyErr_Occurred) is then satisfied too).
def _boom():
    raise RuntimeError("forced shutdown exception")


threading._shutdown = _boom

# Churn thread-states from several worker threads. Each start_new_thread create AND each
# transient thread's exit writes interp->threads.head under HEAD_LOCK -- the racing write.
# Using _thread keeps it cheap and avoids the threading module's own _shutdown bookkeeping.
_running = True


def _noop():
    pass


def churn():
    while _running:
        try:
            _thread.start_new_thread(_noop, ())
        except RuntimeError:
            time.sleep(0)  # transient "can't start new thread" under load; yield and retry


for _ in range(6):
    _thread.start_new_thread(churn, ())

time.sleep(0.1)  # let the churn saturate before we fall into finalization
# Falling off the end -> Py_FinalizeEx -> wait_for_thread_shutdown -> _boom() ->
# handle_thread_shutdown_exception reads interp->threads.head unlocked while churn writes it.
