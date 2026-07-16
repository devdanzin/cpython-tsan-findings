import sys, threading
from collections import OrderedDict
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# OrderedDict tracks insertion order in an internal doubly-linked list whose head
# is od->od_first. `iter(od)`/`list(od)` enters odictiter_new, which reads od_first
# WITHOUT taking the per-object lock. `od.clear()` (OrderedDict_clear_impl, which IS
# @critical_section) calls _odict_clear_nodes, which sets od_first/od_last = NULL and
# frees every node. Reader reads od_first (and then node->key) unlocked while the
# clearing thread nulls it and frees the nodes -> data race / use-after-free.
NT = 8
ROUNDS = 3000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        od = pool[0]
        if wid % 2 == 0:
            od.clear()                    # _odict_clear_nodes: write od_first/od_last=NULL, free nodes
            for i in range(64):
                od[i] = i                 # repopulate so the LL head is non-NULL again
        else:
            try:
                list(od)                  # odict_iter -> odictiter_new: read od_first (unlocked)
            except RuntimeError:
                pass                       # "OrderedDict mutated during iteration" is fine
        leave.wait()

ts = [threading.Thread(target=worker, args=(w,)) for w in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = OrderedDict((i, i) for i in range(64))   # fresh, populated each round
    enter.wait()   # release workers onto the shared od
    leave.wait()   # wait for them to finish this round
for t in ts: t.join()
print("done, no crash")
