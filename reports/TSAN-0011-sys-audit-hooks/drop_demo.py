"""Supplementary: probe the *correctness* consequence of the unsynchronized interp->audit_hooks
lazy init (the fail-open dropped-hook scenario). This is NOT the TSan reproducer (that is repro.py,
which reliably triggers the memory race); this shows the higher-level hook-drop is real but RARE.

N threads register a distinct hook simultaneously (behind a barrier) as the very first hooks, when
interp->audit_hooks is still NULL. If the NULL->PyList_New(0) race is lost such that one thread
appends its hook to a list that is then orphaned by another thread's store, that hook is silently
dropped -> fewer than N hooks fire on a later audit event.

Finding: over 100 process runs at N=256 (free-threaded, PYTHON_GIL=0) this did NOT lose a hook,
because PyList_Append at sysmodule.c:548 re-reads interp->audit_hooks, so the common double-create
outcome is an empty-list *leak*, not a dropped hook; an actual drop needs the narrower interleaving
where one append completes before the other store overwrites the field. The memory-level data race,
by contrast, is certain and TSan-confirmed on every run (see repro.py). Run under a plain
free-threaded build (no TSan needed) in a loop to hunt for the rare drop:

    PYTHON_GIL=0 ./python drop_demo.py    # prints DROPPED=<n>; n>0 == a hook was lost
"""

import sys, threading

assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

N = 256
fired = set()
rec = threading.Lock()
barrier = threading.Barrier(N)


def make_hook(i):
    def hook(event, args):
        if event == "demo.count":
            with rec:
                fired.add(i)
    return hook


hooks = [make_hook(i) for i in range(N)]  # pre-built so only addaudithook is post-barrier
add = sys.addaudithook


def worker(i):
    barrier.wait()
    add(hooks[i])  # tightest window on the NULL->PyList_New(0) lazy-init race


ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join()
sys.audit("demo.count")  # fire every hook that actually made it into the registry
print(f"registered={N} fired={len(fired)} DROPPED={N - len(fired)}")
