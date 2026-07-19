import operator
import threading

# generic sequence iterator (iterobject.c seqiterobject, from iter(obj) on a __getitem__ type):
# iter_iternext writes it->it_index++ (:72) while iter_len reads it->it_index (:100) -> cursor race.
NTHREADS = 8
barrier = threading.Barrier(NTHREADS)


class Seq:
    def __getitem__(self, i):
        if i >= 4096:
            raise IndexError
        return i


def worker(it):
    barrier.wait()
    for _ in range(8000):
        try:
            next(it)  # iter_iternext: it->it_index++
        except (StopIteration, IndexError):
            pass
        operator.length_hint(it, 0)  # iter_len: reads it->it_index


for _ in range(300):
    shared = iter(Seq())  # ONE shared seqiterobject
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
