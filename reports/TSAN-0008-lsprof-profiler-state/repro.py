import sys, threading, time
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"
from profiling.tracing import Profile      # == cProfile.Profile == _lsprof.Profiler

# _lsprof.Profiler keeps ALL of its call-tracking state in the object itself:
# self->currentProfilerContext (a single call-stack), self->profilerEntries (a
# tree), a context freelist and self->flags.  cProfile registers a *global*
# sys.monitoring tool, so while ONE profiler is enabled EVERY thread that runs
# Python drives that one shared state through the profiler's callbacks.
#
# The per-call callbacks + enable/disable/clear are @critical_section-guarded,
# but profiler_dealloc -> flush_unmatched()/clearEntries() are NOT.  So tearing
# a profiler down races (and use-after-frees) against callbacks still in flight
# on other threads.
#
# Layout: N "gen" threads run deep non-tail recursion (a long PY_START burst
# down, then a long PY_RETURN cascade back up) so the shared profiler is
# constantly being driven.  A few "churn" threads rapidly create/enable/disable/
# drop a fresh profiler; each drop deallocs it (flush_unmatched at :984) while a
# gen thread is mid-callback -> TSan data race on self->currentProfilerContext
# (read in flush_unmatched:866 vs write in the callback).

STOP = False

def rec(n):
    if n > 0:
        rec(n - 1)
    return n

def busy():
    while not STOP:
        rec(60)

def churn():
    while not STOP:
        p = Profile()
        try:
            p.enable()
        except ValueError:          # another churn thread holds the tool id
            continue
        p.disable()
        del p                       # -> profiler_dealloc -> flush_unmatched

NGEN = 8
NCHURN = 3
ts = [threading.Thread(target=busy, name="gen%d" % i) for i in range(NGEN)]
ts += [threading.Thread(target=churn, name="churn%d" % i) for i in range(NCHURN)]
for t in ts: t.start()
time.sleep(20)
STOP = True
for t in ts: t.join()
print("done, no race")
