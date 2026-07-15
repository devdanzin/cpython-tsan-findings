import sys, threading
from decimal import Decimal
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# dec_hash caches the hash in self->hash (lazy: -1 sentinel). Many threads hashing the SAME
# freshly-created Decimals race on that cache field (read of the sentinel vs write of the value).
NT = 4
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker():
    for _ in range(ROUNDS):
        enter.wait()
        for d in pool[0]:
            hash(d)              # dec_hash: read self->hash==-1, then write self->hash
        leave.wait()

ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [Decimal(f"{r}.{i}") for i in range(64)]   # fresh, unhashed each round
    enter.wait()   # release workers onto the fresh batch
    leave.wait()   # wait for them to finish this batch
for t in ts: t.join()
print("done, no crash")
