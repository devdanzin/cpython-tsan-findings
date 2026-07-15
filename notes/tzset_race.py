"""Confirmed reproducer: concurrent time.tzset() corrupts libc global timezone state.

time.tzset() is a METH_NOARGS wrapper over libc tzset(), which mutates the process-global tz
state (tzname strings, timezone, daylight) and is not safe for concurrent calls. On a free-
threaded build several threads calling it race in glibc tzset_internal -- one free()s the old
tzname string while another strdup()s a new one -> a libc heap free/malloc race (crash risk).

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
