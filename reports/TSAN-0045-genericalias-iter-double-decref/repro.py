import threading

# A shared GenericAlias iterator (iter(list[int]) -> gaiterobject) is one-shot: ga_iternext reads
# gi->obj and does Py_SETREF(gi->obj, NULL). Two threads both reaching it race gi->obj and
# double-DECREF the old referent (refcount underflow / UAF).
NT = 8


def worker(it, barrier):
    barrier.wait()
    try:
        next(it)  # ga_iternext: Py_SETREF(gi->obj, NULL)
    except StopIteration:
        pass


for _round in range(4000):
    shared = iter(list[int])  # ONE shared gaiterobject
    bar = threading.Barrier(NT)
    threads = [threading.Thread(target=worker, args=(shared, bar)) for _ in range(NT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
