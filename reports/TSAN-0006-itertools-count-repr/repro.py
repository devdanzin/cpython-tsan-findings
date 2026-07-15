import sys, threading, itertools
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A fast-mode itertools.count() keeps its counter in the plain C field lz->cnt.
# count_next() advances it with an ATOMIC compare-exchange (_Py_atomic_compare_exchange_ssize),
# but count_repr() reads the very same field with a PLAIN (non-atomic) load.
# Sharing one count() object across threads -- some calling next(c), some calling repr(c) --
# races the plain read in count_repr against the atomic write in count_next on lz->cnt.
NT = 6                    # worker threads (half repr, half next)
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def repr_worker():
    for _ in range(ROUNDS):
        enter.wait()
        for c in pool[0]:
            repr(c)          # count_repr: plain read of lz->cnt (itertoolsmodule.c:3612)
        leave.wait()

def next_worker():
    for _ in range(ROUNDS):
        enter.wait()
        for c in pool[0]:
            next(c)          # count_next: atomic CAS write of lz->cnt (itertoolsmodule.c:3599)
        leave.wait()

ts = [threading.Thread(target=repr_worker) for _ in range(NT // 2)]
ts += [threading.Thread(target=next_worker) for _ in range(NT - NT // 2)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [itertools.count() for _ in range(64)]   # fresh, fast-mode counts each round
    enter.wait()   # release workers onto the fresh batch
    leave.wait()   # wait for them to finish this batch
for t in ts: t.join()
print("done, no crash")
