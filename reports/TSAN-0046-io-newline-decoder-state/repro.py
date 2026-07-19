import codecs
import io
import threading

# A shared io.IncrementalNewlineDecoder: one thread calls .reset() (writes self->seennl /
# pendingcr in _io_IncrementalNewlineDecoder_reset_impl) while another reads .newlines
# (incrementalnewlinedecoder_newlines_get reads self->seennl) -> data race on the decoder state.
NT = 8
barrier = threading.Barrier(NT)


def worker(dec, role):
    barrier.wait()
    for _ in range(5000):
        if role:
            dec.reset()
        else:
            _ = dec.newlines


for _round in range(200):
    d = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), True)
    d.decode(b"a\r\nb\n")
    ts = [threading.Thread(target=worker, args=(d, i % 2)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
