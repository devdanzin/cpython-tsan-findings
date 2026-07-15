import sys, threading, codecs
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# One MultibyteIncrementalDecoder for an iso2022 codec keeps its 8-byte codec state
# inline in the object (self->state.c). Sharing it across threads is a data race:
#   .reset()    -> iso2022_decode_reset writes self->state.c[0] (_codecs_iso2022.c:314)
#   .getstate() -> _PyLong_FromByteArray reads self->state.c    (longobject.c:1069)
# Neither takes a lock / critical section. We use ONE writer thread (reset) so the
# only possible race partner for the state.c write is a reader's _PyLong_FromByteArray,
# and several reader threads (getstate) to widen the window.
NT_READERS = 5         # getstate readers
ROUNDS = 4000
INNER = 8
pool = [None]
enter = threading.Barrier(NT_READERS + 1 + 1)   # readers + 1 writer + main
leave = threading.Barrier(NT_READERS + 1 + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        d = pool[0]
        for _ in range(INNER):
            d.getstate()           # _PyLong_FromByteArray: read self->state.c  (:1069)
        leave.wait()

def writer():
    for _ in range(ROUNDS):
        enter.wait()
        d = pool[0]
        for _ in range(INNER):
            d.reset()              # iso2022_decode_reset: write self->state.c[0]  (:314)
        leave.wait()

ts = [threading.Thread(target=reader) for _ in range(NT_READERS)]
ts.append(threading.Thread(target=writer))
for t in ts: t.start()
for r in range(ROUNDS):
    d = codecs.getincrementaldecoder("iso2022_jp_2")()
    d.decode(b"\x1b$B")            # designate JISX0208 as G0 so reset() actually mutates c[0]
    pool[0] = d                   # fresh, shared decoder each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
