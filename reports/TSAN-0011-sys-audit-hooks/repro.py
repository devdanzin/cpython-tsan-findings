import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# should_audit (Python/sysmodule.c:239) reads interp->audit_hooks on EVERY audit
# event; sys_addaudithook_impl (Python/sysmodule.c:540) lazily creates that list
# with a PLAIN store the first time any thread calls sys.addaudithook() -- and it
# takes no lock. The first-time store races with concurrent should_audit reads
# (and with sibling addaudithook stores). Audit hooks can't be removed, so adding
# many is harmless; the race is the lazy-init of interp->audit_hooks, so we make
# the very first addaudithook calls collide behind a barrier while other threads
# hammer audit events.

NADD = 24      # threads slamming the first-time lazy-init store (write @540) at once
NAUD = 8       # threads spinning audit events (should_audit read @239)
barrier = threading.Barrier(NADD + NAUD)


def _hook(*a):
    return None


def adder():
    barrier.wait()
    for _ in range(200):
        sys.addaudithook(_hook)          # write interp->audit_hooks (first time) @540


def auditor():
    barrier.wait()
    for _ in range(200000):
        sys.audit("fusil.tsan.test")     # should_audit read of interp->audit_hooks @239


ts = [threading.Thread(target=adder) for _ in range(NADD)]
ts += [threading.Thread(target=auditor) for _ in range(NAUD)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done, no crash")
