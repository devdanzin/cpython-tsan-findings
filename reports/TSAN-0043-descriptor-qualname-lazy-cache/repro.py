import threading

NT = 8

# Gather builtin method/getset/wrapper descriptors whose d_qualname is still NULL, then race
# .__qualname__ across threads on each -- descr_get_qualname lazily writes descr->d_qualname
# with no critical section, so two threads first-reading it write/write-race the cache.
descrs = []
for tp in (str, bytes, list, dict, set, int, float, tuple, frozenset, bytearray):
    for name, v in vars(tp).items():
        if type(v).__name__ in ("method_descriptor", "getset_descriptor", "wrapper_descriptor"):
            descrs.append(v)


def worker(descriptor, barrier):
    barrier.wait()
    for _ in range(20):
        _ = descriptor.__qualname__  # descr_get_qualname: lazy descr->d_qualname write


for _round in range(50):
    for d in descrs:  # each descriptor raced once (d_qualname caches after the first read)
        bar = threading.Barrier(NT)
        threads = [threading.Thread(target=worker, args=(d, bar)) for _ in range(NT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
print("done")
