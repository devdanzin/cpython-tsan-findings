import cProfile
import threading


# cProfile in threads on a free-threaded build -- cpython#126884 (segfaults). The profiler is
# registered as an instrumentation callback; if it is deallocated (profiler_dealloc) while another
# thread's call event still invokes it (ptrace_enter_call -> getEntry -> RotatingTree_Get), the
# freed profiler is used -> use-after-free / SEGV. This driver mirrors the shape (concurrent
# cProfile.runctx); it does not deterministically crash in isolation.
def busy():
    total = 0
    for i in range(3000):
        total += i * i
    return total


def worker():
    for _ in range(300):
        try:
            cProfile.runctx("busy()", {"busy": busy}, {})
        except Exception:
            pass


ts = [threading.Thread(target=worker) for _ in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done")
