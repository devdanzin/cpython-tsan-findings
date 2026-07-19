import threading
NT = 8; ITERS = 200000
def newit(): return iter(set(range(32)))
cell = [newit()]
def worker():
    for _ in range(ITERS):
        it = cell[0]
        try: next(it)
        except StopIteration: cell[0] = newit()
        except Exception: pass
ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
