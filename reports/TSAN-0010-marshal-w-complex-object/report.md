# Data race: `marshal.dumps()` serializes a shared list with unsynchronized reads (`marshal.c:605`)

*`w_complex_object`'s list branch walks a list with `PyList_GET_ITEM(v, i)` — a plain, non-atomic read of `ob_item[]` — and takes no critical section on the list (unlike the set branch a few lines below). When one thread `marshal.dumps()` a list while another thread `append()`s to the same list, the marshaller's non-atomic read of the item array (and of the `ob_item` base pointer) races with `list.append`'s atomic store into that array. `marshal.dumps()` looks read-only to callers, so a shared list is not safe to marshal concurrently with mutation.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Python/marshal.c` serializes a list in `w_complex_object`:

```c
else if (PyList_CheckExact(v)) {
    W_TYPE(TYPE_LIST, p);
    n = PyList_GET_SIZE(v);            /* :602 */
    W_SIZE(n, p);
    for (i = 0; i < n; i++) {          /* :604 */
        w_object(PyList_GET_ITEM(v, i), p);   /* :605  read ob_item[i] (non-atomic) */
    }
}
```

`PyList_GET_ITEM(op, i)` expands to `_PyList_CAST(op)->ob_item[i]` — a plain load of the `ob_item` base pointer followed by an indexed load, with **no critical section** on `v`. Meanwhile another thread calling `list.append()` mutates the very same `ob_item[]` array via `_PyList_AppendTakeRef`, which under free-threading uses an **atomic release store**. Atomic-write vs plain-read on the same location is a data race, reported by ThreadSanitizer.

Note the asymmetry inside the *same function*: the set/frozenset branch (marshal.c:655) wraps its element walk in `Py_BEGIN_CRITICAL_SECTION(v)`, but the list, tuple, and dict branches do not.

## Reproducer

```python
import sys, threading, marshal
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT_MAR = 3          # threads calling marshal.dumps(shared_list)
NT_APP = 3          # threads calling shared_list.append(...)
NT = NT_MAR + NT_APP
ROUNDS = 3000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def marshaller():
    for _ in range(ROUNDS):
        enter.wait()
        lst = pool[0]
        for _ in range(40):
            marshal.dumps(lst)          # w_complex_object: read ob_item[i] non-atomically
        leave.wait()

def appender():
    for _ in range(ROUNDS):
        enter.wait()
        lst = pool[0]
        for i in range(300):
            lst.append(i)               # _PyList_AppendTakeRef: atomic store to ob_item[]
        leave.wait()

ts = [threading.Thread(target=marshaller) for _ in range(NT_MAR)]
ts += [threading.Thread(target=appender) for _ in range(NT_APP)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [0, 1, 2]                 # fresh small list each round: keeps it growing
    enter.wait()                        # release marshallers + appenders onto same list
    leave.wait()                        # wait for the round to finish
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

Reproduces on the first round (exit 66).

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`)

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x7fffb6a95c78 by thread T1:
    #0 w_complex_object Python/marshal.c:605:22   (w_object(PyList_GET_ITEM(v, i), p))
    #1 w_object Python/marshal.c:493:9
    #2 _PyMarshal_WriteObjectToString Python/marshal.c:1923:5
    #3 marshal_dumps_impl Python/marshal.c:2086:12
    ...
    #32 thread_run Modules/_threadmodule.c:388:21

  Previous atomic write of size 8 at 0x7fffb6a95c78 by thread T6:
    #0 _Py_atomic_store_ptr_release Include/cpython/pyatomic_gcc.h:565:3
    #1 list_resize Objects/listobject.c:165:6                       (store new ob_item buffer)
    #2 _PyList_AppendTakeRefListResize Objects/listobject.c:530:9
    #3 _PyList_AppendTakeRef Include/internal/pycore_list.h:53:12
    #4 _PyEval_EvalFrameDefault Python/generated_cases.c.h:3981:27  (_CALL_LIST_APPEND)
    ...
    #27 thread_run Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Python/marshal.c:605:22 in w_complex_object
```

The originally-seeded fleet report caught the sibling variant where the write is the fast-path element store at `pycore_list.h:46` (`_Py_atomic_store_ptr_release(&self->ob_item[len], newitem)`). Both are the same defect on the same `ob_item[]` array; the reproducer above happened to catch the `list_resize` realloc path (write to the `ob_item` *base pointer*), which is the more dangerous variant (see Impact). The read site — `w_complex_object` at `marshal.c:605` — is identical in both, matching the seeded signature.

## Root cause

`w_complex_object` treats the list as immutable during serialization and reads it with the raw `PyList_GET_ITEM` macro:

- `Include/cpython/listobject.h:40` — `#define PyList_GET_ITEM(op, index) (_PyList_CAST(op)->ob_item[(index)])`. This is a plain (non-atomic) read of both the `ob_item` base pointer and the element slot.
- `marshal.c:602-605` — the list is walked with no lock/critical section.

Concurrently, `list.append` mutates the array with proper free-threaded synchronization:

- Fast path `Include/internal/pycore_list.h:46` — `_Py_atomic_store_ptr_release(&self->ob_item[len], newitem)` (writes an element slot).
- Growth path `Objects/listobject.c:165` — `_Py_atomic_store_ptr_release(&self->ob_item, &array->ob_item)` (swaps in a freshly `malloc`'d buffer), then `listobject.c:169` `free_list_items(old_items, ...)` releases the old buffer.

An atomic store on the writer against a plain load on the reader is, by definition, a C-level data race (undefined behavior), which is exactly what TSan flags. The writer side was hardened for free-threading; the marshal reader side was not. The set branch of the *same function* (`marshal.c:655`, `Py_BEGIN_CRITICAL_SECTION(v)`) shows the intended pattern, so this is a missed-conversion gap rather than a design choice to leave it unlocked.

## Impact / severity

Medium. Two cases:

1. **Element-slot race (seeded variant, fast-path append):** the marshaller reads `ob_item[i]` while append writes `ob_item[len]`. A pointer-sized aligned load/store is not torn, so the value read is a valid `PyObject*` (old element, `NULL`, or the new element) — `w_object(NULL, ...)` just emits `TYPE_NULL`. Value-benign, crash-free, but a real data race.

2. **Buffer-pointer / resize race (reproduced variant):** `list_resize` swaps `self->ob_item` to a new buffer and frees the old one (`free_list_items`, `listobject.c:169`). The marshaller may load the *old* `ob_item` base pointer and then index into a buffer that is being freed — a **use-after-free hazard**. In the free-threaded build the free is QSBR-deferred (`free_list_items` -> `_PyMem_FreeDelayed` when the list is shared, `listobject.c:59-65`), which usually delays the free past the read, but the marshaller does not participate in the QSBR read-side protocol (it does not use `_PyList_GetItemRef` or a critical section), so the safety is incidental, not guaranteed. This is the path that makes the bug more than cosmetic.

It requires an unusual usage — mutating a shared list from one thread while marshalling it from another — and does not crash in normal single-writer use. `marshal.dumps()` is a read-only-looking API, so a caller can reasonably (if mistakenly) assume it is safe to marshal a shared list; the hidden non-atomic reads break that under free-threading.

## Suggested fix

Serialize the container branches under a per-object critical section, mirroring the set branch that already does this:

```c
else if (PyList_CheckExact(v)) {
    W_TYPE(TYPE_LIST, p);
    Py_BEGIN_CRITICAL_SECTION(v);
    n = PyList_GET_SIZE(v);
    W_SIZE(n, p);
    for (i = 0; i < n; i++) {
        w_object(PyList_GET_ITEM(v, i), p);
    }
    Py_END_CRITICAL_SECTION();
}
```

The same treatment applies to the dict branch (`marshal.c:608`, `PyDict_Next`) and, defensively, the tuple branch. `Py_BEGIN_CRITICAL_SECTION` compiles to nothing on the default (GIL) build, so there is no cost there; on the free-threaded build it makes the reader mutually exclusive with `list.append`/resize. (A critical section is preferable to switching to `_PyList_GetItemRef` per element because it also gives a consistent size/contents snapshot, matching the set branch's precedent.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, marshalling a shared list argument while sibling workers appended to it. Same "writer converted to atomics, reader still plain" class as many CPython free-threading data races already fixed for other read paths (e.g. `list_repr`, `PyList_GetItemRef`). The whole `w_complex_object` walk (list / tuple / dict) should be audited for the same missing critical section; the set branch is the only container branch currently protected.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
