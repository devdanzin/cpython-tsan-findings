import sys, threading, weakref
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# subtype_getweakref (the `obj.__weakref__` getset) reads the object's weakref
# list head with a PLAIN, unlocked, non-atomic load:
#     weaklistptr = (PyObject **)((char *)obj + type->tp_weaklistoffset);
#     if (*weaklistptr == NULL) ...      // Objects/typeobject.c:4079
# Meanwhile creating a weakref (insert_head) and destroying one
# (clear_weakref_lock_held -> FT_ATOMIC_STORE_PTR(*list, ...), weakrefobject.c:87)
# mutate that SAME list-head slot under LOCK_WEAKREFS. The reader neither locks
# nor uses an atomic load, so reading obj.__weakref__ concurrently with weakref
# creation/destruction on the same shared object is a data race on the list head.

NT = 8
ROUNDS = 3000

def cb(_):
    # A callback makes the weakref a NON-reusable (non-"basic") ref, so each
    # weakref.ref(obj, cb) really allocates + insert_head()s and, when dropped,
    # clear_weakref()s the head -- continuous churn of *weaklistptr.
    pass

pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        obj = pool[0]
        if wid % 2 == 0:
            for _ in range(200):
                obj.__weakref__          # subtype_getweakref: plain read of *weaklistptr
        else:
            for _ in range(200):
                r = weakref.ref(obj, cb) # insert_head on create; clear_weakref on drop
                del r
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = type("Target", (), {})()   # fresh weakref-able object each round
    enter.wait()   # release workers onto the fresh object
    leave.wait()   # wait for them to finish this batch
for t in ts: t.join()
print("done, no crash")
