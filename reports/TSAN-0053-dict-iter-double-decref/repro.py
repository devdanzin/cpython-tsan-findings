import threading

# A shared dict iterator (iter({...}) -> dict_keyiterator) advanced by next() from several threads.
# On exhaustion, dictiter_iternext_threadsafe runs its fail: path:
#     fail:
#         di->di_dict = NULL;   /* non-atomic clear */
#         Py_DECREF(d);         /* drop the iterator's ONE owning ref to the dict */
# and the caller dictiter_iternextkey read `d = di->di_dict` with no lock. Two threads that both
# reach fail: on the same iterator both Py_DECREF(d) -> the dict's refcount underflows -> negative
# refcount / double-free / use-after-free.  (Objects/dictobject.c, main@a1d580430c8.)
#
#   debug-ft-nojit      : SIGABRT, _Py_NegativeRefcount on the dict (dictobject.c:6159), ~8/8 runs.
#   release-ft-nojit-o0 : SIGSEGV (use-after-free) / occasional deadlock from the corrupted dict mutex.
# Free-threaded build only; needs PYTHON_GIL=0.

NT = 8
ITERS = 200_000


def newit():
    return iter({k: k for k in range(32)})


cell = [newit()]


def worker():
    for _ in range(ITERS):
        it = cell[0]
        try:
            next(it)                 # dictiter_iternextkey -> dictiter_iternext_threadsafe
        except StopIteration:
            cell[0] = newit()        # refill so exhaustion (the fail: path) is hit repeatedly
        except Exception:
            pass


threads = [threading.Thread(target=worker) for _ in range(NT)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print("done, no crash")
