import io, sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# io.BufferedReader.detach() IS decorated @critical_section, so its writes
#   self->raw = NULL; self->detached = 1; self->ok = 0;   (Modules/_io/bufferedio.c:626-628)
# run under Py_BEGIN_CRITICAL_SECTION(self).
#
# But buffered_iternext (the tp_iternext slot, reached via next(obj) / iter(obj) / list(obj))
# does its CHECK_INITIALIZED(self) -- which reads self->ok -- at bufferedio.c:1504, BEFORE it
# opens its own Py_BEGIN_CRITICAL_SECTION(self) at :1512. That first flag read is therefore
# unprotected and races detach's self->ok = 0 store (:628).
#
# Reader (plain read of self->ok at :1504) vs writer (CS-protected store at :628) on the same
# int field => TSan data race. detach is one-shot (it invalidates the object), so each round
# uses a FRESH shared BufferedReader lined up on a barrier and raced by iterator + detacher
# threads, so the iternext flag-read and the detach flag-write overlap on the SAME object.

NR = 3                         # iterator threads: for _ in obj  (buffered_iternext CHECK_INITIALIZED)
ND = 3                         # detacher threads: obj.detach()  (self->ok = 0 under CS)
ROUNDS = 4000
DATA = b"line\n" * 4000        # enough lines that iteration stays busy while detach fires

box = [None]
enter = threading.Barrier(NR + ND + 1)
leave = threading.Barrier(NR + ND + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        try:
            for _line in obj:      # -> buffered_iternext -> CHECK_INITIALIZED reads self->ok (:1504)
                pass
        except (ValueError, OSError):
            pass
        leave.wait()

def detacher():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        try:
            obj.detach()           # under CS: self->raw=NULL; self->detached=1; self->ok=0 (:628)
        except (ValueError, OSError):
            pass
        leave.wait()

threads = ([threading.Thread(target=reader) for _ in range(NR)] +
           [threading.Thread(target=detacher) for _ in range(ND)])
for t in threads:
    t.start()

for r in range(ROUNDS):
    raw = io.BytesIO(DATA)
    box[0] = io.BufferedReader(raw)   # fresh shared object each round; ok=1, detached=0
    enter.wait()                      # release iterators + detachers onto the fresh object
    leave.wait()                      # wait for them to finish this round
for t in threads:
    t.join()
print("done, no crash")
