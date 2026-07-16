import sys, threading
from compression import zstd
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# TSAN-0017 was seeded from a vehicle whose worker both CALLS c.flush() and reads
# getattr(c, <name>) over dir(c) -- and dir(ZstdCompressor) includes the read-only
# member "last_mode".  So the actual race captured (see tsan_report.txt) is:
#
#   flush() writes self->last_mode as a PLAIN C store under self->lock
#           (Modules/_zstd/compressor.c:679)  -- write side
#   getattr(c,"last_mode") reads that int via PyMember_GetOne with a RELAXED-ATOMIC
#           load and NO lock (Python/structmember.c:64)  -- read side
#
# Two concurrent flush() calls do NOT race with each other: flush() takes self->lock
# around ALL cctx work and around the last_mode writes, so writer-vs-writer is
# serialized.  The ONLY lock-free access to any compressor field is the member read.
# That makes TSAN-0017 the SAME field / SAME race face as TSAN-0002 (last_mode),
# reached via a different call shape -- a duplicate, not a distinct cctx/buffer bug.
#
# Writers call flush() (write at compressor.c:679); readers hammer getattr(c,"last_mode").
# Fresh compressor each round keeps the failure window tight and both operations valid.

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
