"""Reproducer for the concurrent time.tzset() TSan report -- which is a glibc/TSan FALSE
POSITIVE, not a CPython bug. See notes/tzset_glibc_c_repro.c (the identical race in pure C) and
notes/open-questions-for-umbrella.md for the analysis.

time.tzset() is a METH_NOARGS wrapper over libc tzset(), which rewrites the process-global tz
state (tzname strings). Several threads calling it produce a TSan report in glibc tzset_internal
(one free()s the old tzname string while another strdup()s a new one). BUT glibc serializes
tzset_internal with an internal low-level lock (tzset_lock) that TSan does not model, so the
report is spurious -- the writes are actually serialized, and 800k+ concurrent calls never crash.
The read-only converters (localtime/gmtime/strftime/ctime/asctime) do NOT trip it; only tzset and
mktime (which force a tzset_internal rewrite) do.

Run (free-threaded + TSan build):
    DEBUGINFOD_URLS= setarch -R env PYTHON_GIL=0 \
      TSAN_OPTIONS="halt_on_error=1 symbolize=1 history_size=4" \
      ./python tzset_race.py

Confirmed output (CPython 3.16.0a0, --disable-gil --with-thread-sanitizer):
    Write (T?): free   <- tzset_internal (time/tzset.c:401) <- cfunction_vectorcall_NOARGS
    Prev  (T?): malloc <- strdup        (string/strdup.c)   <- cfunction_vectorcall_NOARGS
    SUMMARY: ThreadSanitizer: data race ... in free
    (exit 66 = TSan detected)
"""

import sys, threading, time

assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0 on a --disable-gil build"

N = 200_000
NT = 4
start = threading.Barrier(NT)


def worker():
    start.wait()
    for _ in range(N):
        time.tzset()  # libc tzset_internal mutates global tzname (strdup old / free new)


ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done, no crash (TSan reports the race above)")
