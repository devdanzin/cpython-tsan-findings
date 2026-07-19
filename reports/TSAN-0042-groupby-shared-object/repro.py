import itertools
import threading

# A shared groupby iterator: several threads drive groupby_next concurrently (via list()),
# racing gbo->currkey / currvalue / currgrouper (advanced with no per-object lock).
NTHREADS = 8
barrier = threading.Barrier(NTHREADS)


def worker(gb):
    barrier.wait()
    try:
        list(gb)  # list_extend -> groupby_next on the shared gb
    except (RuntimeError, StopIteration, ValueError, TypeError):
        pass


for _ in range(500):
    shared = itertools.groupby(range(8192))  # ONE shared groupby
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
