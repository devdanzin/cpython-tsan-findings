import threading, gc
fs = frozenset(range(6))
NT = 8; ITERS = 100000
cell = [iter(fs)]
def worker(role):
    for i in range(ITERS):
        it = cell[0]
        try: next(it)
        except StopIteration: cell[0] = iter(fs)
        except Exception: pass
        if role == 2 and i % 32 == 0: gc.collect()
ts = [threading.Thread(target=worker, args=(i%3,)) for i in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
