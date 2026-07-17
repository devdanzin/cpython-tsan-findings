import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# _PyEval_NoToolsForUnwind (Python/ceval.c:2465) -> no_tools_for_local_event (Python/ceval.h)
# does a PLAIN 1-byte read of
#       code->_co_monitoring->active_monitors.tools[PY_MONITORING_EVENT_PY_UNWIND]
# from the eval loop / gen_close, holding NO lock at all.
#
# Meanwhile force_instrument_lock_held (Python/instrumentation.c:1842) does
#       code->_co_monitoring->active_monitors = active_events;
# a PLAIN 16-byte struct assignment (_Py_LocalMonitors = uint8_t tools[16]), which the
# compiler emits as two 8-byte stores. It runs under LOCK_CODE(code) -- the code object's
# critical section -- which excludes other *writers* but NOT the lock-free eval-loop reader.
#
# To make the version go stale (so threads lazily call _Py_Instrument from RESUME and
# take the writer path) we churn sys.monitoring.set_events on a shared generator's code
# object while other threads drive that same generator to close.
#
#   writer: thread in gen_send_ex2 -> RESUME version check -> _Py_Instrument -> 8-byte store
#   reader: thread in gen_close    -> _PyEval_NoToolsForUnwind -> 1-byte load of tools[13]
#
# tools[13] (PY_MONITORING_EVENT_PY_UNWIND) sits INSIDE the 8-byte word covering tools[8..15]
# that the writer replaces -- exactly the fleet vehicle's ...528 (write) / ...52d (read).

NW = 4  # worker threads driving generators to close
ROUNDS = 4000

TOOL = 2
EV = sys.monitoring.events


def g():
    # A plain generator with no try block: after next(), it is suspended at the RESUME
    # following YIELD_VALUE with exception depth 1, which is what gen_close's
    # _PyEval_NoToolsForUnwind fast-path check looks at.
    yield 1
    yield 2


def cb(*args):
    return None


sys.monitoring.use_tool_id(TOOL, "tsan0036")
for e in (EV.PY_UNWIND, EV.PY_RETURN, EV.PY_RESUME):
    sys.monitoring.register_callback(TOOL, e, cb)

stop = threading.Event()
enter = threading.Barrier(NW + 1)


def worker():
    enter.wait()
    while not stop.is_set():
        for _ in range(50):
            it = g()
            next(it)        # RESUME -> stale version -> _Py_Instrument -> WRITE active_monitors
            it.close()      # gen_close -> _PyEval_NoToolsForUnwind -> READ tools[13]


threads = [threading.Thread(target=worker) for _ in range(NW)]
for t in threads:
    t.start()

enter.wait()
try:
    # Churn the global monitoring version: every change invalidates every code object's
    # _co_instrumentation_version, so each worker's next RESUME re-enters _Py_Instrument
    # and rewrites active_monitors on the SHARED code object g.__code__.
    for r in range(ROUNDS):
        sys.monitoring.set_events(TOOL, EV.PY_UNWIND | EV.PY_RETURN)
        sys.monitoring.set_events(TOOL, EV.PY_RESUME)
        sys.monitoring.set_events(TOOL, 0)
finally:
    stop.set()
    for t in threads:
        t.join()
    sys.monitoring.set_events(TOOL, 0)
    sys.monitoring.free_tool_id(TOOL)
print("done, no crash")
