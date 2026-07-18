import sys, threading, itertools
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
NT = 8
def worker(it, bar, advance):
    bar.wait()
    for _ in range(400):
        try:
            next(it) if advance else repr(it)
        except Exception: pass
for _r in range(ROUNDS):
    it = itertools.count(10**18, 2)   # big-int -> SLOW mode (long_cnt)
    bar = threading.Barrier(NT)
    ts = [threading.Thread(target=worker, args=(it, bar, i % 2 == 0)) for i in range(NT)]
    for t in ts: t.start()
    for t in ts: t.join()
print("done")
