import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# Race on PyFrameObject.f_trace between two threads that share the SAME live frame:
#
#   WRITE  Python/sysmodule.c:1125  trace_trampoline
#            Py_XSETREF(frame->f_trace, result)      -- runs on the frame's OWNING
#            thread while it executes traced code; takes NO frame critical section.
#   READ   Objects/frameobject.c:1155  frame_trace_opcodes_set_impl
#            if (self->f_trace)                       -- runs when ANOTHER thread does
#            `frame.f_trace_opcodes = True`; the setter IS @critical_section, but the
#            trampoline write above is not, so the lock protects nothing.
#
# A tracer whose trace fn returns a callable makes the trampoline re-store
# frame->f_trace on every LINE event.  Mutator threads reach into the tracer
# threads' live frames via sys._current_frames() and set f_trace_opcodes,
# reading f_trace concurrently.

NTRACE = 4
NMUT   = 4
ROUNDS = 400
LINES  = 800

stop = threading.Event()
start = threading.Barrier(NTRACE + NMUT)


def tracer(frame, event, arg):
    # Returning a callable -> trace_trampoline does Py_XSETREF(frame->f_trace, ...)
    # (the write at sysmodule.c:1125) on every traced event in this frame.
    return tracer


def busy():
    x = 0
    for _ in range(LINES):
        x += 1          # one LINE event per iteration -> one trampoline f_trace write
    return x


def tracer_worker():
    start.wait()
    sys.settrace(tracer)        # per-thread: only traces THIS thread's frames
    try:
        for _ in range(ROUNDS):
            busy()
    finally:
        sys.settrace(None)


def mutator_worker():
    start.wait()
    while not stop.is_set():
        for f in list(sys._current_frames().values()):
            try:
                f.f_trace_opcodes = True    # frame_trace_opcodes_set_impl: reads self->f_trace
                f.f_trace_opcodes = False
            except Exception:
                pass


ts = [threading.Thread(target=tracer_worker, name="tsan_t%d" % i) for i in range(NTRACE)]
ms = [threading.Thread(target=mutator_worker, name="tsan_m%d" % i) for i in range(NMUT)]
for t in ts + ms:
    t.start()
for t in ts:
    t.join()
stop.set()
for m in ms:
    m.join()
print("done, no crash")
