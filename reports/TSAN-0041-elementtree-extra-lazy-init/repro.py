import threading
import xml.etree.ElementTree as ET

# A shared Element whose `extra` struct is still NULL: concurrent `.attrib` reads each
# hit `if (!self->extra) create_extra(...)` and race the unlocked `self->extra = malloc()`.
NTHREADS = 8
ITERS = 4000
barrier = threading.Barrier(NTHREADS)


def worker(elem):
    barrier.wait()
    for _ in range(ITERS):
        _ = elem.attrib  # element_attrib_getter -> create_extra (lazy, unlocked)
        _ = len(elem)  # element_length reads self->extra


for _ in range(200):
    shared = ET.Element("tag")  # fresh: extra == NULL until first attrib/child access
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
