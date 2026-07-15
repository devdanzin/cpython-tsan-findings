import sys, os, threading
import _multiprocessing
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# Reproduces the TSAN-0003 signature (semaphore.c:516 _multiprocessing_SemLock_impl
# vs object.c:3319 _Py_Dealloc). NOTE: this is a TSan FALSE POSITIVE rooted in glibc,
# not a CPython bug -- see report.md.
#
# _multiprocessing.SemLock(kind, value, maxvalue, name, unlink)
#   kind: RECURSIVE_MUTEX=0, SEMAPHORE=1
# Creating a SemLock calls glibc sem_open()  -> tsearch()  (INSERT into the process-global
#   __sem_mappings tree; malloc a node).
# Destroying it (dealloc -> SEM_CLOSE) calls sem_close() -> tdelete() (REMOVE from that same
#   tree; free the node).
# Many threads creating+destroying SemLocks concurrently make tsearch/tdelete touch the shared
# tree backbone at the same time. glibc actually serializes this with its internal
# __sem_mappings_lock (an lll/futex lock), which TSan does not model -> TSan reports a data race.
SEMAPHORE = 1
NT = 8
ROUNDS = 4000
enter = threading.Barrier(NT)


def worker(tid):
    enter.wait()
    for i in range(ROUNDS):
        name = "/fu-%d-%d-%d" % (os.getpid(), tid, i)
        try:
            sl = _multiprocessing.SemLock(SEMAPHORE, 1, 1, name, True)
        except (FileExistsError, OSError):
            continue
        # drop the only reference -> semlock_dealloc -> sem_close -> tdelete
        del sl


ts = [threading.Thread(target=worker, args=(t,)) for t in range(NT)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done, no crash")
