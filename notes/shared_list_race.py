# Concurrent unsynchronized access to a shared builtin `list` on a free-threaded build:
# one thread UNPACKs it (reads ob_size) while another appends/pops (stores ob_size).
# No user-level lock -> ThreadSanitizer reports a data race on the list's size word.
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0 on a --disable-gil build"

shared = [0, 1, 2]
N = 200_000
start = threading.Barrier(2)

def unpacker():
    start.wait()
    for _ in range(N):
        try:
            a, b, c = shared          # UNPACK_SEQUENCE reads Py_SIZE(shared)
        except ValueError:
            pass                      # size changed under us; not what we're testing

def mutator():
    start.wait()
    for _ in range(N):
        shared.append(0)              # list_resize stores Py_SIZE(shared)
        shared.pop()

t1 = threading.Thread(target=unpacker)
t2 = threading.Thread(target=mutator)
t1.start(); t2.start(); t1.join(); t2.join()
print("done, no crash (TSan reports the race above)")
