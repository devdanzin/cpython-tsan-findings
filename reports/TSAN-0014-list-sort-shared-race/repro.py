"""Reproducer for TSAN-0014 (concurrent list.sort() of a shared list), minimized from the fleet
vehicle with shrinkray (994 lines / 32.9 kB -> ~28 lines).

email._header_value_parser.Comment subclasses `list`, so sharing one instance across threads and
hammering all of its methods concurrently invokes list.sort() -- a writer whose `binarysort`
rewrites the list's backing array *in place* with NO per-object critical section -- alongside
readers that iterate the list (list_get_item_ref / _Py_TryXGetRef). That sort-vs-read collision
is the race.

Probabilistic: ~15-30% of single runs trip it (the sort-vs-read window is narrow), so run it a
few times / in a loop to observe it reliably. (The un-minimized fleet vehicle reproduces 100% but
is 994 lines; minimizing traded determinism for size.)

    for i in $(seq 5); do
      DEBUGINFOD_URLS= PYTHON_GIL=0 \
        TSAN_OPTIONS="halt_on_error=1:symbolize=1:exitcode=66:history_size=4" \
        setarch -R ./python repro.py 2>&1 | grep -q "in binarysort" && { echo "hit on run $i"; break; }
    done

SUMMARY: ThreadSanitizer: data race Objects/listobject.c:1918 in binarysort  (exit 66)
"""

import email._header_value_parser
import threading as _tsan_threading

fuzz_target_module = email._header_value_parser
_tsan_shared_args = ([1, 2],)
_tsan_shared = []
try:
    _tsan_shared.append(getattr(fuzz_target_module, 'Comment')())
except Exception:
    ...
_WORKERS_PER_OBJ = 4
_ITERS = 200


def _tsan_worker(_idx, _wid):
    _obj = _tsan_shared[_idx]
    _names = [n for n in dir(_obj)]
    for _i in range(_ITERS):
        for _ in range(2):
            try:
                _m = getattr(_obj, _names[_i % len(_names)])
                _m(*_tsan_shared_args[: _i % 3])
            except Exception:
                ...


_tsan_threads = []
for _idx in range(len(_tsan_shared)):
    for _wid in range(_WORKERS_PER_OBJ):
        _tsan_threads.append(_tsan_threading.Thread(target=_tsan_worker, args=(_idx, _wid)))
for _t in _tsan_threads:
    _t.start()
for _t in _tsan_threads:
    _t.join()
