# Data race: `obj.__weakref__` (`subtype_getweakref`) reads the weakref list head without a lock (`typeobject.c:4079`)

*`subtype_getweakref` — the getset behind `obj.__weakref__` — reads the object's weakref list head (`*weaklistptr`, at `obj + tp_weaklistoffset`) with a plain, unlocked, non-atomic load, and hands the raw head pointer to `Py_NewRef`. Meanwhile creating a weakref (`insert_head`) or destroying one (`clear_weakref_lock_held` → `FT_ATOMIC_STORE_PTR`) mutates that same slot under `LOCK_WEAKREFS`. On a free-threaded build, reading `obj.__weakref__` concurrently with `weakref.ref(obj)` / weakref teardown on the same shared object is a data race on the list head. The rest of `weakrefobject.c` was FT-hardened (per-object lock + atomics); this reader was missed.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

Every object that supports weak references stores a single "weakref list head" pointer in the slot at `obj + tp_weaklistoffset` (the `__weakref__` field). In the free-threaded build, all of the weakref machinery synchronizes access to that slot with a per-object lock (`LOCK_WEAKREFS`) and atomic stores (`FT_ATOMIC_STORE_PTR`):

- `insert_head` / `insert_weakref` mutate `*list` under `LOCK_WEAKREFS(obj)` (creation, `get_or_create_weakref:434/445/455`).
- `clear_weakref_lock_held` mutates `*list` under the lock **and** via `FT_ATOMIC_STORE_PTR(*list, self->wr_next)` (teardown, `weakrefobject.c:87`).
- `_PyWeakref_GetWeakrefCount` reads `*GET_WEAKREFS_LISTPTR(obj)` under `LOCK_WEAKREFS(obj)` (`weakrefobject.c:48`).

But `subtype_getweakref` — the C getset that implements the `obj.__weakref__` attribute — reads that same slot with a **plain C dereference, no lock and no atomic load**:

```c
static PyObject *
subtype_getweakref(PyObject *obj, void *context)
{
    PyObject **weaklistptr;
    PyObject *result;
    ...
    weaklistptr = (PyObject **)((char *)obj + type->tp_weaklistoffset);
    if (*weaklistptr == NULL)          /* :4079  plain read, unsynchronized */
        result = Py_None;
    else
        result = *weaklistptr;         /* :4082  plain read */
    return Py_NewRef(result);          /* :4083  incref of a possibly-dying head weakref */
}
```

Two threads — one reading `obj.__weakref__`, another creating/destroying a weakref to the *same* shared `obj` — race on the list head. TSan reports it deterministically (exit 66).

## Reproducer

```python
import sys, threading, weakref
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 8
ROUNDS = 3000

def cb(_):
    # A callback makes the weakref a NON-reusable ("non-basic") ref, so each
    # weakref.ref(obj, cb) really allocates + insert_head()s and, when dropped,
    # clear_weakref()s the head -- continuous churn of *weaklistptr.
    pass

pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        obj = pool[0]
        if wid % 2 == 0:
            for _ in range(200):
                obj.__weakref__          # subtype_getweakref: plain read of *weaklistptr
        else:
            for _ in range(200):
                r = weakref.ref(obj, cb) # insert_head on create; clear_weakref on drop
                del r
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = type("Target", (), {})()   # fresh weakref-able object each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2170147)
  Write of size 8 at 0x7fffb6554810 by thread T2:
    #0 insert_head            Objects/weakrefobject.c:323:11   (*list = newref)
    #1 insert_weakref         Objects/weakrefobject.c:392:9
    #2 get_or_create_weakref  Objects/weakrefobject.c:445:9    (inside LOCK_WEAKREFS)
    #3 weakref___new__        Objects/weakrefobject.c:474:28
    ...
    #32 thread_run            Modules/_threadmodule.c:388:21

  Previous read of size 8 at 0x7fffb6554810 by thread T1:
    #0 subtype_getweakref     Objects/typeobject.c:4079:9      (if (*weaklistptr == NULL))  <-- plain load
    #1 getset_get             Objects/descrobject.c:194:16
    #2 _PyObject_GenericGetAttrWithDict Objects/object.c:1926:19
    #3 PyObject_GenericGetAttr Objects/object.c:2012:12         (obj.__weakref__)
    ...
    #28 thread_run            Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Objects/typeobject.c:4079:9 in subtype_getweakref
```

Reproduces deterministically (exit 66) on every run. The write side that collides with the read varies by scheduling: **creation** (`insert_head`, above) and **teardown** (`clear_weakref_lock_held` → `FT_ATOMIC_STORE_PTR`, `weakrefobject.c:87`, which is the write side captured in the original fleet vehicle `multiprocessing.managers`). Both are the same list-head slot; the SUMMARY always points at `subtype_getweakref:4079`.

## Root cause

The free-threaded weakref subsystem protects the per-object list head with a striped per-object mutex (`WEAKREF_LIST_LOCK(obj)` via `LOCK_WEAKREFS`) plus `FT_ATOMIC_STORE_PTR` for the head write. `subtype_getweakref` predates / was missed by that hardening: it reads `*weaklistptr` with an ordinary load and no lock. So a reader of `obj.__weakref__` and a concurrent `weakref.ref(obj)` (insert) or weakref deallocation (clear) on the same object have no happens-before relationship on the list-head slot — a data race.

The read is not just a torn-word concern. `subtype_getweakref` returns the head weakref object itself (`result = *weaklistptr; return Py_NewRef(result)`), so it grabs a raw pointer to a list node and increfs it **without the lock and without `_Py_TryIncref`**. If the collision is with weakref teardown (the vehicle's `clear_weakref` from `weakref_dealloc`), the head weakref it just read can be the very object another thread is deallocating — a potential use-after-free / incref-of-a-dying-object window. Contrast the FT-safe pattern already used a few lines away in the same file: `try_reuse_basic_ref` guards its reuse with `_Py_TryIncref` under `LOCK_WEAKREFS` (`weakrefobject.c:349`), and `_PyWeakref_GetWeakrefCount` reads the head under `LOCK_WEAKREFS` (`weakrefobject.c:48`).

## Impact / severity

Low-to-medium. The reliably-observed symptom is the TSan-reported data race on `obj.__weakref__` — an operation callers treat as a read-only attribute fetch on a shared object, which is a legitimate free-threaded use (weakref creation and inspection of a shared object are supported concurrent operations). Because the read hands the unsynchronized head pointer to `Py_NewRef` with no lock/try-incref, it is additionally a *plausible* use-after-free when it collides with concurrent weakref teardown, rather than being purely value-benign. No crash was observed in the TSan build (freed memory is not immediately reused there), so the concrete confirmed impact is the data race; the UAF is the worst-case.

## Suggested fix

Read (and incref) the head under the per-object weakref lock, mirroring `_PyWeakref_GetWeakrefCount` / `try_reuse_basic_ref`, and use `_Py_TryIncref` so a head weakref that is mid-teardown is not resurrected:

```c
weaklistptr = (PyObject **)((char *)obj + type->tp_weaklistoffset);
LOCK_WEAKREFS(obj);
PyObject *head = *weaklistptr;               /* now race-free w.r.t. insert/clear */
if (head != NULL && _Py_TryIncref(head)) {
    result = head;
} else {
    result = Py_NewRef(Py_None);
}
UNLOCK_WEAKREFS(obj);
return result;
```

A minimal `FT_ATOMIC_LOAD_PTR(*weaklistptr)` would silence the torn-read/TSan race, but on its own it does **not** close the incref-of-a-dying-weakref window — the lock + `_Py_TryIncref` form is the correct fix. (`LOCK_WEAKREFS`/`_Py_TryIncref` compile to no-ops on the default GIL build.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`); fleet vehicle `fusil-tsan_fleet_02/inst-01/.../multiprocessing_managers-...-tsanNEW` (there the colliding write was the teardown face, `clear_weakref_lock_held:87`). Reproduced here stdlib-only with `weakref.ref(obj, cb)` + `obj.__weakref__`. This looks like a straggler from the weakref free-threading hardening (the gh-116738 audit class): the writers and the count/reuse readers are locked, but the `__weakref__` getset reader was not. Worth auditing sibling plain reads of `*weaklistptr` (e.g. any other direct `tp_weaklistoffset` dereference outside `LOCK_WEAKREFS`).

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
