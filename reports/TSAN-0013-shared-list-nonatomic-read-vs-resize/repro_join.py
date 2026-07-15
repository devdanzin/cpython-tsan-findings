"""Confirmed reproducer: bytes_join reading a shared list another thread is growing.

Same "concurrent unsynchronized access to a shared builtin" class as shared_list_race.py:
one thread appends to a shared list (list_resize publishes the new ob_item with an atomic
release store) while another thread b"".join()s it (bytes_join reads the items with a plain,
non-atomic PyList_GET_ITEM). TSan flags the atomic-store-vs-plain-read on the list's ob_item.

Run (free-threaded + TSan build):
    DEBUGINFOD_URLS= setarch -R env PYTHON_GIL=0 \
      TSAN_OPTIONS="halt_on_error=1 symbolize=1 history_size=4" \
      ./python bytes_join_race.py

Confirmed signature (CPython 3.16.0a0, --disable-gil --with-thread-sanitizer):
    Atomic write: list_resize -> _Py_atomic_store_ptr_release  (Objects/listobject.c:165)
    Prev read   : stringlib_bytes_join                         (Objects/stringlib/join.h:63)
    -> Include/cpython/pyatomic_gcc.h:_Py_atomic_store_ptr_release | Objects/stringlib/join.h:stringlib_bytes_join
"""

import sys, threading

assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0 on a --disable-gil build"

shared = [b"x" * 8 for _ in range(64)]
N = 100_000
start = threading.Barrier(2)


def mutator():
    start.wait()
    for _ in range(N):
        shared.append(b"y" * 8)  # _PyList_AppendTakeRefListResize -> list_resize stores ob_item
        shared.pop()


def joiner():
    start.wait()
    for _ in range(N):
        try:
            b"".join(shared)  # bytes_join_impl -> stringlib_bytes_join reads the items
        except (TypeError, ValueError):
            pass


t1 = threading.Thread(target=mutator)
t2 = threading.Thread(target=joiner)
t1.start()
t2.start()
t1.join()
t2.join()
print("done, no crash (TSan reports the race above)")
