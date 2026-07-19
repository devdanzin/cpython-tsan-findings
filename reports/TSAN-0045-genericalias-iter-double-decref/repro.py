import threading

# A shared GenericAlias iterator (iter(list[int]) -> gaiterobject) is one-shot: ga_iternext reads
# gi->obj and does Py_SETREF(gi->obj, NULL) (Objects/genericaliasobject.c:952) with no lock. Two
# threads both reaching it race gi->obj and double-DECREF the old referent -> refcount underflow /
# use-after-free. Under TSan: exit 66. On a plain free-threaded build (no TSan): SIGSEGV at
# ga_iternext, deterministically and near-instantly (crashes within the first few rounds).
NT = 16


def worker(it, barrier):
    barrier.wait()
    try:
        next(it)  # ga_iternext: Py_SETREF(gi->obj, NULL)
    except StopIteration:
        pass


for _round in range(20000):
    shared = iter(list[int])  # ONE shared gaiterobject; gi->obj refcount == 1
    bar = threading.Barrier(NT)
    threads = [threading.Thread(target=worker, args=(shared, bar)) for _ in range(NT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
