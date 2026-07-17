# TSAN-0014 clean synthetic: isolate binarysort-vs-read with a barrier.
# Re-scramble the shared list ONLY while readers are parked (no race there); then release
# readers to iterate WHILE the sorter sorts -> list_sort_impl's in-place binarysort writes
# race list_get_item_ref's atomic slot reads. Plain list, no subclass, no key= (so sort
# rewrites the list's own backing array in place).
import sys, threading
assert not sys._is_gil_enabled(), "need --disable-gil + PYTHON_GIL=0"

SZ = 2000
ROUNDS = 1500
NR = 4
# a fixed scrambled order, no `random` import (multiplicative-hash permutation)
SCRAMBLED = sorted(range(SZ), key=lambda x: (x * 2654435761) & 0xFFFFFFFF)
L = list(SCRAMBLED)

enter = threading.Barrier(NR + 1)
leave = threading.Barrier(NR + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        for _x in L:            # list_get_item_ref -> _Py_TryXGetRef(&ob_item[i])
            pass
        leave.wait()

def main_sorter():
    for _ in range(ROUNDS):
        L[:] = SCRAMBLED        # re-scramble while readers are parked at enter (no reader active)
        enter.wait()            # release readers
        L.sort()                # in-place binarysort/merge, racing the readers
        leave.wait()

ts = [threading.Thread(target=reader) for _ in range(NR)]
for t in ts: t.start()
main_sorter()
for t in ts: t.join()
print("done")
