import operator
import threading

# A shared set iterator: some threads advance it (setiter_iternext) while others read its
# cursor via operator.length_hint (setiter_len) -> data race on the non-atomic countdown/index.
NTHREADS = 8
ITERS = 20000
barrier = threading.Barrier(NTHREADS)


def worker(it):
    barrier.wait()
    for _ in range(ITERS):
        try:
            next(it)  # setiter_iternext: advance the shared cursor
        except StopIteration:
            pass
        operator.length_hint(it, 0)  # setiter_len: read the shared cursor


for _ in range(300):
    shared = iter(set(range(4096)))  # ONE shared set iterator
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
