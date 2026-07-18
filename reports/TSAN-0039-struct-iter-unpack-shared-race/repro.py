import operator
import struct
import sys
import threading

# struct.Struct.iter_unpack form of the builtin-iterator shared-cursor race (cpython#154013): many
# threads share ONE unpack iterator; half advance it (unpackiter_iternext writes the index,
# Modules/_struct.c:2278) and half read its cursor via __length_hint__ (unpackiter_len reads the
# index, :2249) -> non-atomic index data race. Run under the debug-ft-nojit-tsan build with
# PYTHON_GIL=0; exit 66 = TSan race.
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NT = 8
N = 4096
S = struct.Struct("i")


def advance(it, bar):
    bar.wait()
    for _ in it:
        pass


def measure(it, bar):
    bar.wait()
    for _ in range(N):
        operator.length_hint(it, 0)


for _r in range(ROUNDS):
    shared = S.iter_unpack(bytes(4 * N))
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
