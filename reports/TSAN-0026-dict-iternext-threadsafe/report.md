# Data race: the "threadsafe" dict iterator reads `ma_values` non-atomically (`dictobject.c:6043`)

*`dictiter_iternext_threadsafe` — the lock-free free-threaded dict-iterator `next` — decides split-vs-combined with `if (_PyDict_HasSplitTable(d))`, a macro that does a **plain** read of `d->ma_values` (`dictobject.c:6043`). A concurrent `setattr()` that overflows an instance's full shared keys converts its `__dict__` split→combined inside `dictresize`, publishing the new `ma_values` (NULL) with an **atomic release store** (`set_values`, `dictobject.c:215`). Plain read vs atomic store on the same word is a TSan data race — and the very next line (6044) already reads `ma_values` atomically, so this is an incomplete atomic conversion, not the general "don't share a dict" class.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`dictiter_iternext_threadsafe` (`Objects/dictobject.c`, `#ifdef Py_GIL_DISABLED`) is the deliberately lock-free iterator-`next` for the free-threaded build. It marks the dict shared (`ensure_shared_on_read`) and reads its fields with atomics so that concurrent readers need no critical section. But its split-table test uses the plain macro:

```c
i = _Py_atomic_load_ssize_relaxed(&di->di_pos);
k = _Py_atomic_load_ptr_acquire(&d->ma_keys);
assert(i >= 0);
if (_PyDict_HasSplitTable(d)) {                                   /* :6043  PLAIN read of ma_values */
    PyDictValues *values = _Py_atomic_load_ptr_consume(&d->ma_values);  /* :6044  ATOMIC read of ma_values */
    ...
```

`_PyDict_HasSplitTable(d)` expands (`Include/internal/pycore_dict.h:56`) to:

```c
#define _PyDict_HasSplitTable(d) ((d)->ma_values != NULL)
```

— a plain, non-atomic load of `d->ma_values`. Meanwhile a writer that converts the dict from a split table to a combined one publishes the new value pointer atomically (`set_values` → `_Py_atomic_store_ptr_release(&mp->ma_values, NULL)`). TSan flags the atomic-release-store-vs-plain-read pair on the `ma_values` word. Because line **6044** reads the *same field* atomically (`_Py_atomic_load_ptr_consume`), the plain read on line **6043** is an oversight in an otherwise-completed atomic conversion.

## Reproducer

```python
import sys
from threading import Barrier, Thread
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

N_ITER = 3
N_MUT = 3
ROUNDS = 6000
BASE = [f"a{i}" for i in range(30)]   # 30 == SHARED_KEYS_MAX_SIZE -> fills shared keys, dk_usable->0
LIVE = BASE[:15]
box = [None]
enter = Barrier(N_ITER + N_MUT + 1)
leave = Barrier(N_ITER + N_MUT + 1)

class C:
    pass

# Prime the type's shared keys to full so no fresh instance can extend them:
# after this, insert_split_key() returns DKIX_EMPTY on any new key and the
# first setattr goes straight to dictresize -> set_values(NULL).
for _ in range(3):
    _p = C()
    for _a in BASE:
        setattr(_p, _a, 0)
    _p.__dict__

def make_split_instance():
    obj = C()
    for a in LIVE:
        setattr(obj, a, 0)     # all present in shared keys -> stays SPLIT
    obj.__dict__               # materialize split dict
    return obj

def iterator_worker():
    for _ in range(ROUNDS):
        enter.wait()
        d = box[0].__dict__
        try:
            for _k in d:                     # dictiter_iternext_threadsafe: HasSplitTable read @6043
                pass
        except RuntimeError:
            pass                             # "changed size during iteration" = expected
        leave.wait()

def mutator_worker():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        for n in range(6):
            try:
                setattr(obj, f"z{n}", n)     # new key, shared keys full -> dictresize -> set_values(NULL) @215
            except Exception:
                pass
        leave.wait()

threads = ([Thread(target=iterator_worker) for _ in range(N_ITER)] +
           [Thread(target=mutator_worker) for _ in range(N_MUT)])
for t in threads: t.start()
for r in range(ROUNDS):
    box[0] = make_split_instance()
    enter.wait(); leave.wait()
for t in threads: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

Reproduces in **~0.7 s**, deterministically, exit **66**, no crash.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2175183)
  Atomic write of size 8 at 0x7fffb6ab65e8 by thread T6:
    #0 _Py_atomic_store_ptr_release  Include/cpython/pyatomic_gcc.h:565:3   (mp->ma_values <- NULL)
    #1 set_values                    Objects/dictobject.c:215:5
    #2 dictresize                    Objects/dictobject.c:2222:9            (set_values(mp, NULL))
    #3 insertion_resize              Objects/dictobject.c:1869:12
    #4 insert_combined_dict          Objects/dictobject.c:1886:13
    #5 insertdict                    Objects/dictobject.c:2009:13
    ... setitem_take2_lock_held ... _PyDict_SetItem_LockHeld
    ... store_instance_attr_lock_held ... store_instance_attr_dict
    ... PyObject_SetAttr <- builtin_setattr   (setattr(obj, "zN", n))

  Previous read of size 8 at 0x7fffb6ab65e8 by thread T3:
    #0 dictiter_iternext_threadsafe  Objects/dictobject.c:6043:9            (if (_PyDict_HasSplitTable(d)))
    #1 dictiter_iternextkey          Objects/dictobject.c:5749:9
    #2 _PyForIter_VirtualIteratorNext Python/ceval.c:3774:22
    #3 _PyEval_EvalFrameDefault      Python/generated_cases.c.h:6379:36     (FOR_ITER over obj.__dict__)
    ...

SUMMARY: ThreadSanitizer: data race Objects/dictobject.c:6043:9 in dictiter_iternext_threadsafe
```

(The seed vehicle reached the read through `dictiter_iternextitem` (`.items()`); the reproducer reaches it through `dictiter_iternextkey` (`for k in d`). Same racing pair — `set_values`/`_Py_atomic_store_ptr_release` vs `dictiter_iternext_threadsafe:6043` — the signature matches. TSan names either the read or the write in the SUMMARY line depending on scheduling; both frames are always present.)

## Root cause

The value pointer `ma_values` is treated as an atomically-published field in the free-threaded build:

- **Writer** (`Objects/dictobject.c:211-216`):
  ```c
  static inline void
  set_values(PyDictObject *mp, PyDictValues *values)
  {
      ASSERT_OWNED_OR_SHARED(mp);
      _Py_atomic_store_ptr_release(&mp->ma_values, values);   /* :215 */
  }
  ```
  `dictresize` (`:2222`) calls `set_values(mp, NULL)` when it converts a split table into a combined one (`oldvalues != NULL`). That conversion is triggered from `store_instance_attr_lock_held` (`:7396`): `insert_split_key` returns `DKIX_EMPTY` once the type's shared keys are full (`dk_usable == 0`, capped at `SHARED_KEYS_MAX_SIZE == 30`), so a *new* attribute falls through to `_PyDict_SetItem_LockHeld` → `insertdict` → `insert_combined_dict` → `insertion_resize` → `dictresize`. The writer holds the dict's critical section (`Py_BEGIN_CRITICAL_SECTION(dict)` in `store_instance_attr_dict:7481`).

- **Reader** (`Objects/dictobject.c:6038-6044`):
  ```c
  ensure_shared_on_read(d);
  i = _Py_atomic_load_ssize_relaxed(&di->di_pos);
  k = _Py_atomic_load_ptr_acquire(&d->ma_keys);
  assert(i >= 0);
  if (_PyDict_HasSplitTable(d)) {                              /* :6043  ((d)->ma_values != NULL) — PLAIN */
      PyDictValues *values = _Py_atomic_load_ptr_consume(&d->ma_values);   /* :6044  ATOMIC */
  ```
  The reader takes **no** critical section — that is by design (`ensure_shared_on_read`'s comment: *"necessary to safely allow concurrent reads without locking"*). It reads `di_pos`, `ma_keys` and (at 6044) `ma_values` with atomics, but the split-table branch decision on line **6043** reads `ma_values` through the plain `_PyDict_HasSplitTable` macro. That plain load, concurrent with the writer's `_Py_atomic_store_ptr_release`, is the data race TSan reports.

It is value-benign in practice: the branch outcome is re-validated on the atomic path. If line 6043 reads a stale non-NULL pointer it enters the split branch, then line 6044's atomic consume-load observes NULL and `goto concurrent_modification` raises `RuntimeError`; if 6043 reads NULL it takes the combined branch. Either way the read is a single aligned word and no torn/dangling pointer is dereferenced. But it is a genuine C11 data race in a function whose entire purpose is to be safe under concurrent access.

## Impact / severity

**Low.** Value-benign, crash-free across repeated runs (the raced value is an aligned pointer that the code re-reads atomically one line later). It is not a use-after-free: the split values array itself is retired via QSBR (`ensure_shared_on_read` marks the dict shared precisely so the old keys/values are freed safely). The defect is a residual, unsynchronized read inside the code path CPython advertises as the lock-free thread-safe iterator, so it undermines the "safe to iterate a shared dict concurrently" guarantee that path is meant to provide, and it is TSan-noisy for anyone running the FT build under the sanitizer.

## Suggested fix

Read `ma_values` **once, atomically**, and branch on that value — the atomic consume-load already on line 6044 can simply be hoisted above the split-table test:

```c
PyDictValues *values = _Py_atomic_load_ptr_consume(&d->ma_values);
if (values != NULL) {                 /* was: if (_PyDict_HasSplitTable(d)) */
    ...                               /* split-table branch, reuse `values` */
}
else {
    ...                               /* combined-table branch */
}
```

This removes the plain macro read at the one lock-free call site while leaving the (lock-held) callers of `_PyDict_HasSplitTable` untouched. Alternatively, make `_PyDict_HasSplitTable` itself perform an atomic load under `Py_GIL_DISABLED` so every reader is covered; that is heavier (the macro has ~15 call sites, most under a lock) but systemically safer.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 02. This is the **same defect class** ruled a bug by Thomas Wouters (Yhg1s, CPython RM) on 2026-07-15 — *"the non-atomic reader should use the atomic accessor the writer already uses"* (see `notes/shared-builtin-concurrent-access.md`, and **TSAN-0013** for the shared-`list` faces). It is, however, a **distinct instance**, and a clearer one:

- It is dict-specific (`ma_values` in the managed-dict split/combined machinery), not a `list` `ob_item`/`ob_size` read.
- The racing read lives *inside* `dictiter_iternext_threadsafe`, a routine explicitly written to be the lock-free FT iterator — so this is an **incomplete atomic conversion**, not "you shouldn't have shared this object." The proof is on the adjacent line: 6044 already reads `ma_values` with `_Py_atomic_load_ptr_consume`; only the 6043 macro read was missed.

Related but separate: while overflowing the same shared instance dict, the iterator also races `ma_used` (`get_index_from_order:674` `assert(mp->ma_used <= SHARED_KEYS_MAX_SIZE)` vs `STORE_USED` in `store_instance_attr_lock_held:7460`) — another non-atomic read in the same lock-free path, same underlying pattern; not the signature reported here. Worth auditing `dictiter_iternext_threadsafe` end-to-end for any remaining plain reads of dict fields the writers publish atomically. Cross-check gh-116738 (the builtin free-threading audit): `Objects/dictobject.c` split-table readers are in scope.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
