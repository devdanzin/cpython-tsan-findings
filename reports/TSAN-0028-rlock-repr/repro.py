import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# threading.RLock() is _thread.RLock (the C type). acquire()/release() store the owner
# thread-id into the recursive mutex via _Py_atomic_store_ullong_relaxed (lock.c:439/466),
# but rlock_repr reads self->lock.thread with a PLAIN load (_threadmodule.c:1291). A thread
# repr()-ing a shared RLock races with another thread acquiring/releasing it.
NA = 3          # acquirer threads (store m->thread on acquire/release)
NR = 3          # reprer threads   (plain read of m->thread)
NLOCKS = 32
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NA + NR + 1)
leave = threading.Barrier(NA + NR + 1)

def acquirer():
    for _ in range(ROUNDS):
        enter.wait()
        for lk in pool[0]:
            lk.acquire()        # _PyRecursiveMutex_LockTimed: atomic store m->thread = tid
            lk.release()        # _PyRecursiveMutex_TryUnlock:  atomic store m->thread = 0
        leave.wait()

def reprer():
    for _ in range(ROUNDS):
        enter.wait()
        for lk in pool[0]:
            repr(lk)            # rlock_repr: plain read of self->lock.thread  (:1291)
        leave.wait()

ts = [threading.Thread(target=acquirer) for _ in range(NA)]
ts += [threading.Thread(target=reprer) for _ in range(NR)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [threading.RLock() for _ in range(NLOCKS)]  # fresh shared locks each round
    enter.wait()   # release workers onto the fresh batch
    leave.wait()   # wait for them to finish this batch
for t in ts: t.join()
print("done, no crash")
