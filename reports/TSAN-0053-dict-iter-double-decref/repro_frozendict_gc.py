import threading, gc

# TSAN-0053 pattern (shared dict iterator double-DECREF) applied to a LONG-LIVED
# frozendict: the double-decref doesn't go immediately negative (refcount>1) but the
# concurrent gc.collect() catches "refcount too small" in validate_gc_objects.
fd = frozendict({4:'FREE',1:'LOCAL',3:'GLOBAL_IMPLICIT',2:'GLOBAL_EXPLICIT',5:'CELL'})
NT = 8
ITERS = 100000
cell = [iter(fd)]
def worker(role):
    for i in range(ITERS):
        it = cell[0]
        try:
            next(it)
        except StopIteration:
            cell[0] = iter(fd)
        except Exception:
            pass
        if role == 2 and i % 32 == 0:
            gc.collect()
ts = [threading.Thread(target=worker, args=(i%3,)) for i in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
