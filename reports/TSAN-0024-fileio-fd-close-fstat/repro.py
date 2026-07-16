import sys, threading, tempfile, os
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A shared unbuffered file object (FileIO). close() -> internal_close writes self->fd = -1;
# fileno() -> _io_FileIO_fileno_impl reads self->fd. The int field is touched with no atomic
# and no critical section (FileIO has none), so a thread closing the shared FileIO races with
# a thread reading its descriptor.
path = tempfile.NamedTemporaryFile(delete=False).name
with open(path, "wb") as f:
    f.write(b"x" * 64)

NT = 8
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        f = pool[0]
        try:
            if wid % 2 == 0:
                f.close()       # internal_close: self->fd = -1        (write)
            else:
                f.fileno()      # _io_FileIO_fileno_impl: read self->fd (read)
        except (ValueError, OSError):
            pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = open(path, "rb", buffering=0)   # fresh, open FileIO each round
    enter.wait()
    leave.wait()
    pool[0].close()
for t in ts: t.join()
os.unlink(path)
print("done, no crash")
