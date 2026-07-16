import sys, threading, gc
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# One SHARED base type. Every worker concurrently CREATES short-lived subclasses of
# it (type() -> PyType_Ready -> add_subclass, which registers the new subclass in
# base->tp_subclasses under the interpreter types-mutex) AND drops them + forces GC
# (type_dealloc -> remove_subclass -> clear_tp_subclasses, which writes/clears
# base->tp_subclasses WITHOUT taking that mutex). The two paths race on the shared
# base's tp_subclasses field.

class Base:
    pass

NT = 8
ROUNDS = 6000
barrier = threading.Barrier(NT)

def worker():
    barrier.wait()
    for _ in range(ROUNDS):
        # create fresh short-lived subclasses of the SHARED Base  -> add_subclass
        s = type("S", (Base,), {})
        del s
        # force the dying subclass through type_dealloc -> remove_subclass, whose
        # empty-dict branch does clear_tp_subclasses(base) with no lock, concurrently
        # with other threads' add_subclass reading base->tp_subclasses.
        gc.collect()

ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done, no crash")
