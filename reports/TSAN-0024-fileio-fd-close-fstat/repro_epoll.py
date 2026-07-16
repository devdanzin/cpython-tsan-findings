import sys, threading, select
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A shared select.epoll() object. close() writes self->epfd = -1 (pyepoll_internal_close);
# fileno() reads self->epfd (select_epoll_fileno_impl). Both touch the plain int field with
# no atomic and no shared critical section (only close() is @critical_section), so a thread
# closing the shared epoll races with a thread reading its fd.
NT = 8
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        ep = pool[0]
        try:
            if wid % 2 == 0:
                ep.close()      # pyepoll_internal_close: self->epfd = -1   (write)
            else:
                ep.fileno()     # select_epoll_fileno_impl: read self->epfd (read)
        except (ValueError, OSError):
            pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = select.epoll()          # fresh, open epoll each round
    enter.wait()                      # release workers onto it
    leave.wait()                      # wait for them
    pool[0].close()                   # ensure the fd is reclaimed
for t in ts: t.join()
print("done, no crash")
