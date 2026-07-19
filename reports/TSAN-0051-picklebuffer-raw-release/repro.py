import pickle
import threading

# A shared pickle.PickleBuffer: one thread reads its view (.raw() -> picklebuf_raw) while another
# releases it (.release() -> picklebuf_release -> PyBuffer_Release) -> data race on the Py_buffer.
NT = 8
barrier = threading.Barrier(NT)


def worker(pb, role):
    barrier.wait()
    for _ in range(4000):
        try:
            if role:
                pb.raw()
            else:
                pb.release()
        except (BufferError, ValueError):
            pass


for _round in range(400):
    buf = pickle.PickleBuffer(bytearray(64))
    ts = [threading.Thread(target=worker, args=(buf, i % 2)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
