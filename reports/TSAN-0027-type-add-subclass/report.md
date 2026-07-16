# Data race: a base type's `tp_subclasses` registry is written lock-free during subclass dealloc while a concurrent subclass creation reads it under the type lock (`typeobject.c:add_subclass` vs `clear_tp_subclasses`)

*Creating a subclass runs `add_subclass(base, type)` under the per-interpreter types mutex (`BEGIN_TYPE_LOCK` in `PyType_Ready`), reading `base->tp_subclasses`. Destroying a subclass runs `remove_subclass(base, type)` from `type_dealloc` with **no** lock, and its empty-dict branch calls `clear_tp_subclasses(base)` which writes `base->tp_subclasses`. Two threads — one creating a subclass of a shared base, one GC-deallocating another subclass of the same base — race on that base's shared `tp_subclasses` field. The read path locks it; the dealloc path does not.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

A base type's list of live subclasses is stored in `base->tp_subclasses` (a dict, lazily allocated). It is mutated from two directions during normal class lifecycle:

- **Creation** — `type(...)` → `PyType_Ready` → `type_ready_add_subclasses` → `add_subclass(base, type)` registers the new subclass. This runs under `BEGIN_TYPE_LOCK()` (the per-interpreter `interp->types.mutex`), taken in `PyType_Ready` (`Objects/typeobject.c:9619`).
- **Destruction** — the subclass's `type_dealloc` → `type_dealloc_common` → `remove_all_subclasses` → `remove_subclass(base, type)` unregisters it, and when the dict becomes empty calls `clear_tp_subclasses(base)`, which does `Py_CLEAR(base->tp_subclasses)`. **This path takes no type lock.**

On a free-threaded build, thread A creating a subclass of `Base` (locked read of `Base->tp_subclasses`) races thread B garbage-collecting a *different* dead subclass of the same `Base` (lock-free write of `Base->tp_subclasses`). `Base` is shared internal state, and the creation path clearly intends the field to be lock-protected — the dealloc path is missing the same protection.

The GC write really is concurrent with the running world: in `gc_collect_internal`, `delete_garbage()` (which drives `type_dealloc`) runs **after** `_PyEval_StartTheWorld()` (`Python/gc_free_threading.c:2161` then `:2176`), so other threads are live while a dying type unregisters itself from its base.

## Reproducer

```python
import sys, threading, gc
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# One SHARED base type. Every worker concurrently CREATES short-lived subclasses of
# it (type() -> PyType_Ready -> add_subclass, which registers the new subclass in
# base->tp_subclasses under the interpreter types-mutex) AND drops them + forces GC
# (type_dealloc -> remove_subclass -> clear_tp_subclasses, which writes/clears
# base->tp_subclasses WITHOUT taking that mutex). The two paths race on the shared
# base's tp_subclasses field.

class Base:
    pass

NT = 8
ROUNDS = 6000
barrier = threading.Barrier(NT)

def worker():
    barrier.wait()
    for _ in range(ROUNDS):
        s = type("S", (Base,), {})   # add_subclass(Base, s)  [locked read of Base->tp_subclasses]
        del s
        gc.collect()                 # type_dealloc(s) -> remove_subclass(Base, s)
                                     # -> clear_tp_subclasses(Base) [lock-free write]

ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

Reproduces reliably: 3/3 runs, exit 66, in ~5–24 s.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2172405)
  Read of size 8 at 0x7fffb6ab6a88 by thread T6:
    #0 lookup_tp_subclasses  Objects/typeobject.c            (return base->tp_subclasses)
    #1 add_subclass          Objects/typeobject.c:9701:28    (subclasses = lookup_tp_subclasses(base))
    #2 type_ready_add_subclasses Objects/typeobject.c:9397:32
    #3 type_ready            Objects/typeobject.c:9574:9
    #4 PyType_Ready          Objects/typeobject.c:9622:15    (under BEGIN_TYPE_LOCK, :9619)
    #5 type_new_impl         Objects/typeobject.c:4953:9
    #6 type_new              Objects/typeobject.c:5106:12
    ...
    #34 thread_run           Modules/_threadmodule.c:388:21

  Previous write of size 8 at 0x7fffb6ab6a88 by thread T3:
    #0 clear_tp_subclasses   Objects/typeobject.c:728:5      (Py_CLEAR(base->tp_subclasses))
    #1 remove_subclass       Objects/typeobject.c:9784:9     (if size==0: clear_tp_subclasses(base))
    #2 remove_all_subclasses Objects/typeobject.c:9798:13
    #3 type_dealloc_common   Objects/typeobject.c:6847:9
    #4 type_dealloc          Objects/typeobject.c:7006:5     (NO type lock held)
    #5 _Py_Dealloc           Objects/object.c:3319:5
    ...
    #9 tuple_dealloc         Objects/tupleobject.c:277:9     (dying subclass's mro/bases tuple)
    ...
    #13 delete_garbage       Python/gc_free_threading.c      (world already RESTARTED, :2176)
    #14 gc_collect_internal  Python/gc_free_threading.c:2176:5
    ...
    #18 gc_collect           Modules/clinic/gcmodule.c.h:143:21

SUMMARY: ThreadSanitizer: data race Objects/typeobject.c in lookup_tp_subclasses
```

Same two functions as the fleet-seeded report (line numbers identical). Exit code 66.

## Root cause

Both the read and the write touch the same 8-byte field, `base->tp_subclasses`.

Read — `lookup_tp_subclasses` (called by `add_subclass`, `Objects/typeobject.c:9701`):

```c
static inline PyObject *
lookup_tp_subclasses(PyTypeObject *self)
{
    if (self->tp_flags & _Py_TPFLAGS_STATIC_BUILTIN) { ... }
    return (PyObject *)self->tp_subclasses;      /* plain read of the field */
}
```

This read happens under the type lock: `PyType_Ready` does `BEGIN_TYPE_LOCK()` (`:9619`, i.e. `Py_BEGIN_CRITICAL_SECTION_MUTEX(&interp->types.mutex)`) before calling `type_ready` → `type_ready_add_subclasses` → `add_subclass`.

Write — `clear_tp_subclasses` (`Objects/typeobject.c:728`), reached from `remove_subclass` (`:9784`) when the registry empties:

```c
static void
remove_subclass(PyTypeObject *base, PyTypeObject *type)
{
    PyObject *subclasses = lookup_tp_subclasses(base);   // borrowed
    ...
    if (PyDict_Size(subclasses) == 0) {
        clear_tp_subclasses(base);                       // :9784
    }
}

static void
clear_tp_subclasses(PyTypeObject *self)
{
    if (self->tp_flags & _Py_TPFLAGS_STATIC_BUILTIN) { ... }
    Py_CLEAR(self->tp_subclasses);                       /* :728  plain write of the field */
}
```

The entire dealloc chain — `type_dealloc` → `type_dealloc_common` (`:6847`) → `remove_all_subclasses` → `remove_subclass` → `clear_tp_subclasses` — takes **no** type lock. `type_dealloc` untracks the object and calls `type_dealloc_common` directly (`Objects/typeobject.c:7005–7006`) with nothing guarding `base`'s shared registry.

Crucially, the dealloc is driven by the free-threaded cyclic GC *with the world running*: `gc_collect_internal` restarts the world (`_PyEval_StartTheWorld`, `Python/gc_free_threading.c:2161`) before it calls `delete_garbage()` (`:2176`). `delete_garbage` runs `tp_clear` and cascades deallocs; here a dead subclass's `__mro__`/`__bases__` tuple is decref'd (`tuple_dealloc`), dropping the subclass's last reference and running `type_dealloc`, which unregisters it from the *shared, still-live* `Base`. Meanwhile another thread is mid-`add_subclass` on the same `Base`, holding the type lock — but the lock buys nothing because the dealloc side never acquires it.

`add_subclass` even documents that GC can mutate `base->tp_subclasses` under it ("`PyWeakref_NewRef()` can trigger a garbage collection which can execute arbitrary Python code and so modify `base->tp_subclasses`"), and re-reads the field afterwards — but that comment addresses *same-thread* reentrancy, not a *concurrent* GC thread clearing the field with no lock.

## Impact / severity

**Medium.** More than a benign value race:

- The racing field is a pointer, and the write side is `Py_CLEAR` — it drops the dict's last reference and can *free* the subclasses dict. If `add_subclass` on another thread has just read that same (now-being-freed) pointer as non-NULL, its subsequent `PyDict_SetItem(subclasses, key, ref)` operates on a borrowed reference to a dict that a concurrent `clear_tp_subclasses` is tearing down — a use-after-free / crash window on the shared registry, not merely a torn read. (The window is narrow: it requires the base's registry to hit size 0 exactly as a new subclass is being added.)
- Even without a free, the lock-free write vs locked read means an in-flight subclass registration can be lost or observe an inconsistent field.

Observed manifestation here is a clean TSan data-race abort (exit 66, no segfault in these runs), consistent with the value usually being consistent — but the UAF potential makes it worth fixing rather than suppressing. The trigger is ordinary Python: a shared base class with subclasses being created and garbage-collected on different threads.

Scope: this is **in scope** for the FT-race campaign. It is *not* the out-of-scope "don't concurrently construct one shared object" case (cpython#127192) — here the racing threads construct/destroy *distinct* subclasses; the shared state is the base type's `tp_subclasses` registry, which the creation path already protects with the type lock. The asymmetry (locked add, lock-free remove) is a genuine synchronization gap in shared type state.

## Suggested fix

Make the dealloc-side mutation of `base->tp_subclasses` take the same type lock the add side uses. `remove_subclass` (or `remove_all_subclasses` / `type_dealloc_common`) should run its `lookup_tp_subclasses` / `PyDict_DelItem` / `clear_tp_subclasses` sequence under `BEGIN_TYPE_LOCK()` on the interpreter types mutex, mirroring `PyType_Ready`'s `add_subclass`:

```c
static void
remove_subclass(PyTypeObject *base, PyTypeObject *type)
{
    BEGIN_TYPE_LOCK();
    PyObject *subclasses = lookup_tp_subclasses(base);
    if (subclasses != NULL) {
        assert(PyDict_CheckExact(subclasses));
        PyObject *key = get_subclasses_key(type, base);
        if (key != NULL && PyDict_DelItem(subclasses, key)) {
            PyErr_Clear();
        }
        Py_XDECREF(key);
        if (PyDict_Size(subclasses) == 0) {
            clear_tp_subclasses(base);   /* now serialized against add_subclass */
        }
    }
    END_TYPE_LOCK();
}
```

(Or push the lock up to `type_dealloc_common` so the whole `remove_all_subclasses` loop is covered.) The lock acquisition must be re-entrancy- and deadlock-safe with respect to the surrounding dealloc; `Py_BEGIN_CRITICAL_SECTION_MUTEX` is designed for exactly this. Alternatively, since the reads already use `lookup_tp_subclasses`, the field itself could be made atomic (`FT_ATOMIC` load/store on `tp_subclasses`) so at minimum the pointer read/write is well-defined — but that alone does not close the free/UAF window on the dict, so locking the remove path is the more complete fix.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet `fusil-tsan_fleet_02`. Confirmed unfixed on `heads/main:bcf98ddbc40` (current main, Jul 2026). This belongs to the free-threading type-object audit family (cf. gh-116738, the meta-issue for making static/heap type state FT-safe); `tp_subclasses` mutation on the dealloc path is not covered by the add-side type-lock protection. The same audit should check the other `tp_subclasses` mutators (`init_tp_subclasses`, the `_PyStaticType_*` variants) for symmetric lock coverage. Distinct from cpython#127192 (concurrent construction of a single shared object).

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
