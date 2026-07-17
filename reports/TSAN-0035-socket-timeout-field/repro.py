import socket, sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# PySocketSockObject.sock_timeout (Modules/socketmodule.h:347, a PyTime_t == int64_t) is
# per-socket mutable state that Python-level methods both write and read with PLAIN,
# non-atomic accesses:
#
#   sock_setblocking      socketmodule.c:3172  s->sock_timeout = _PyTime_FromSeconds(...)   WRITE
#   sock_gettimeout_impl  socketmodule.c:3308  if (s->sock_timeout < 0)                      READ
#
# Neither is atomic and neither takes a critical section, so gettimeout() on a socket another
# thread is calling setblocking() on is a data race on that field.
#
# This is an INCOMPLETE free-threading conversion, not an oversight in isolation: the sibling
# field sock_fd on the SAME struct was given relaxed-atomic accessors (get_sock_fd/set_sock_fd,
# socketmodule.c:567-592) by gh-128277/PR#128304, and the module-global state->defaulttimeout --
# a PyTime_t, the exact same type as sock_timeout -- was made atomic by gh-116616/PR#116623.
# sock_timeout is the one mutable per-socket scalar that was left plain.
#
# One writer + N readers, so the only pair TSan can report is the read/write pair we want.
# No network needed: an unconnected socket has a sock_timeout like any other.

NR = 4          # reader threads: gettimeout() -- plain read
ROUNDS = 20000

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
start = threading.Barrier(NR + 1)
stop = threading.Event()

def reader():
    start.wait()
    while not stop.is_set():
        s.gettimeout()              # -> sock_gettimeout_impl: plain read of s->sock_timeout

def writer():
    start.wait()
    for n in range(ROUNDS):
        # setblocking(False) -> sock_timeout = 0; setblocking(True) -> sock_timeout = -1s
        s.setblocking(n & 1)        # -> sock_setblocking: plain write of s->sock_timeout
    stop.set()

threads = [threading.Thread(target=reader) for _ in range(NR)]
threads.append(threading.Thread(target=writer))
for t in threads:
    t.start()
for t in threads:
    t.join()
s.close()
print("done, no crash")
