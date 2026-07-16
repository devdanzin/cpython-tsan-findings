import sys, threading, readline
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# readline stores the completer callback in its per-module state (readlinestate.completer,
# a PyObject*).  readline.set_completer() is @critical_section (locks the module) and writes
# the pointer via set_hook()/Py_XSETREF (readline.c:480).  readline.get_completer() is NOT
# @critical_section and reads state->completer with a plain, unsynchronized load
# (readline.c:899).  A getter thread racing a setter thread is a data race on the shared
# module-state pointer -- and, because the getter reads-then-Py_NewRef()s a pointer the setter
# may concurrently drop the last reference to, a latent use-after-free.
NT = 6                       # total worker threads
ROUNDS = 4000
go = [False]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def setter():
    for _ in range(ROUNDS):
        enter.wait()
        # Fresh closure each round so the previous completer's refcount actually drops
        # (widens the UAF window) and every call is a real pointer write.
        fn = lambda text, state: None
        readline.set_completer(fn)
        readline.set_completer(None)   # churn: clear then re-set next round
        leave.wait()

def getter():
    for _ in range(ROUNDS):
        enter.wait()
        readline.get_completer()       # reads state->completer unsynchronized (readline.c:899)
        readline.get_completer()
        leave.wait()

ts = [threading.Thread(target=setter if i % 2 == 0 else getter) for i in range(NT)]
for t in ts: t.start()
for _ in range(ROUNDS):
    enter.wait()   # release workers
    leave.wait()   # wait for the batch
for t in ts: t.join()
print("done, no crash")
