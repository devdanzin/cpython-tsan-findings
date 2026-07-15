import sys, threading, codecs
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A MultibyteIncrementalDecoder keeps its incomplete-input state in three plain C fields:
#   unsigned char pending[8]; Py_ssize_t pendingsize; MultibyteCodec_State state;
# getstate() READS self->pending / self->pendingsize; reset() WRITES self->pendingsize = 0.
# Neither takes a lock / critical section, so sharing ONE decoder across threads and calling
# getstate() on some while reset() runs on others is a data race on self->pendingsize.
NT = 8
ROUNDS = 4000
box = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        dec = box[0]
        for _ in range(6):
            try:
                dec.getstate()      # reads self->pendingsize (multibytecodec.c:1261)
            except Exception:
                pass
            try:
                dec.reset()         # writes self->pendingsize = 0 (multibytecodec.c:1346)
            except Exception:
                pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts:
    t.start()

factory = codecs.getincrementaldecoder("euc_jp")
for r in range(ROUNDS):
    dec = factory()
    dec.decode(b"\xa4")             # partial euc_jp lead byte -> pendingsize=1, pending non-empty
    box[0] = dec                    # publish the shared decoder for this round
    enter.wait()                    # release workers onto it
    leave.wait()                    # wait for them to finish this batch
for t in ts:
    t.join()
print("done, no crash")
