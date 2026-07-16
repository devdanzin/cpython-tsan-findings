import sys, threading
from decimal import Context
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A single decimal.Context shared across threads. Its C struct embeds an
# mpd_context_t whose `status` field (uint32_t) is read/written with plain,
# unsynchronized accesses:
#   ctx.clear_flags()  -> _decimal_Context_clear_flags_impl: CTX(self)->status = 0   (write, _decimal.c:1421)
#   repr(ctx)          -> context_repr:  mpd_lsnprint_signals(..., ctx->status, ...) (read,  _decimal.c:1570)
# Concurrent writers (clear_flags) and readers (repr) race on ctx->status.
NT = 8
ITERS = 200_000
ctx = Context()
barrier = threading.Barrier(NT)

def writer():
    barrier.wait()
    for _ in range(ITERS):
        ctx.clear_flags()          # write ctx->status = 0

def reader():
    barrier.wait()
    for _ in range(ITERS):
        repr(ctx)                  # read ctx->status (renders the flags)

ts = [threading.Thread(target=(writer if i % 2 == 0 else reader)) for i in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
