import operator
import sys
import threading

# str-iterator form of cpython#153928: many threads share ONE str iterator; half advance it
# (unicode_ascii_iter_next writes it->it_index, Objects/unicodeobject.c:14983) and half read its
# cursor via __length_hint__ (unicodeiter_len reads it->it_index, :14997) -> non-atomic it_index
# data race (+ it_seq double-DECREF on exhaustion, by inspection, same as the bytes analog
# TSAN-0037). Run under the debug-ft-nojit-tsan build with PYTHON_GIL=0; exit 66 = TSan race.
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NT = 8
LEN = 4096


def advance(it, bar):
    bar.wait()
    for _ in it:
        pass


def measure(it, bar):
    bar.wait()
    for _ in range(LEN):
        operator.length_hint(it, 0)


for _r in range(ROUNDS):
    shared = iter("A" * LEN)
    bar = threading.Barrier(NT)
    ts = [
        threading.Thread(target=(advance if i % 2 else measure), args=(shared, bar))
        for i in range(NT)
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
