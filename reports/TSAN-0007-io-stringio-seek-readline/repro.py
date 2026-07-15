import sys, threading, io
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# io.StringIO's explicit methods (seek/read/readline/write/truncate/...) are all
# @critical_section-protected, but the tp_iternext slot (stringio_iternext) is NOT.
# It reads/advances self->pos via _stringio_readline WITHOUT taking the per-object
# critical section. So iterating a shared StringIO (readlines()/list()/for-line-in)
# races with a concurrent seek() that DOES hold the critical section:
#   writer: _io_StringIO_seek_impl   self->pos = pos          (stringio.c:543, write)
#   reader: _stringio_readline       self->pos >= string_size (stringio.c:365, read)
#           <- stringio_iternext <- list.extend(iter) <- readlines()

NT_SEEK = 3          # threads writing self->pos via seek() (critical-section held)
NT_ITER = 3          # threads reading self->pos via iteration (NO critical section)
NT = NT_SEEK + NT_ITER
ROUNDS = 3000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)


def seeker():
    for _ in range(ROUNDS):
        enter.wait()
        s = pool[0]
        for _ in range(40):
            s.seek(0)              # _io_StringIO_seek_impl: writes self->pos (:543)
        leave.wait()


def iterator():
    for _ in range(ROUNDS):
        enter.wait()
        s = pool[0]
        for _ in range(40):
            s.seek(0)              # reset (protected) so readlines yields the full buffer
            s.readlines()          # -> stringio_iternext -> _stringio_readline reads self->pos (:365)
        leave.wait()


ts = [threading.Thread(target=seeker) for _ in range(NT_SEEK)]
ts += [threading.Thread(target=iterator) for _ in range(NT_ITER)]
for t in ts:
    t.start()
for r in range(ROUNDS):
    pool[0] = io.StringIO("line\n" * 200)   # fresh shared StringIO each round
    enter.wait()   # release all workers onto the shared object
    leave.wait()   # wait for them to finish this batch
for t in ts:
    t.join()
print("done, no crash")
