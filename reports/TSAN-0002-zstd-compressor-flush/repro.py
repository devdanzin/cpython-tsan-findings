import sys, threading
from compression import zstd
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A ZstdCompressor exposes its `last_mode` field as a READ-ONLY Py_T_INT member.
# ZstdCompressor.flush()/.compress() write self->last_mode as a PLAIN C store while
# holding self->lock (Modules/_zstd/compressor.c:679).  But getattr(c, "last_mode")
# reads that same int through PyMember_GetOne, which uses a RELAXED ATOMIC load
# (Python/structmember.c:64) and does NOT take self->lock.  So on a shared compressor,
# a plain unlocked write races an unlocked atomic read of last_mode -> TSan data race.
#
# Writers call flush() (write at compressor.c:679); readers hammer getattr(c,"last_mode").
# Fresh compressor each round keeps the window tight and both operations valid.

NT = 8            # threads per shared compressor (even = flush/write, odd = read)
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        c = pool[0]
        if wid % 2 == 0:
            c.flush()                      # _zstd_ZstdCompressor_flush_impl: self->last_mode = mode
        else:
            for _ in range(16):
                getattr(c, "last_mode")    # PyMember_GetOne: relaxed-atomic read of last_mode
        leave.wait()

ts = [threading.Thread(target=worker, args=(w,)) for w in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = zstd.ZstdCompressor()        # fresh, unlocked compressor each round
    enter.wait()     # release the batch of workers onto the shared compressor
    leave.wait()     # wait for them to finish this batch
for t in ts: t.join()
print("done, no crash")
