import sys, threading
# Bytes-iterator analog of cpython#153928: many threads share ONE bytes iterator and
# advance its cursor concurrently. striter_next (Objects/bytesobject.c) reads it->it_index
# (bounds check) and writes it->it_index++ with no synchronization -> data race + OOB read;
# on exhaustion two threads both run `it->it_seq = NULL; Py_DECREF(seq)` -> double-DECREF UAF.
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NT = 8
LEN = 2000

def drain(it, b):
    b.wait()
    for _ in it:
        pass

for r in range(ROUNDS):
    it = iter(b"A" * LEN)
    b = threading.Barrier(NT)
    ts = [threading.Thread(target=drain, args=(it, b)) for _ in range(NT)]
    for t in ts: t.start()
    for t in ts: t.join()
print("done")
