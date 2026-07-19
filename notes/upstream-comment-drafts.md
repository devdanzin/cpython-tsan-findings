# Draft upstream comments (fleet-10 findings) — awaiting maintainer review before posting

Three drafted comments confirming existing issues with independent `fusil --tsan` reproducers.
All reproduced on `main@a1d580430c8` (3.16.0a0) on **both** `debug-ft-nojit-tsan` and
`release-ft-nojit-tsan` (release + TSan), so none are debug-only. Outward-facing — post at the
maintainer's discretion.

---

## → gh-149816 (or on PR #149918) — `_elementtree` item (69)

Independent confirmation of item **(69) "Unsynchronized extra pointer dereference in len in `Modules/_elementtree.c`"**, found by ThreadSanitizer fuzzing (`fusil --tsan`). Worth noting the race is a bit broader than a read-deref: it's a **write/write** on the lazily-allocated `self->extra`, because the `if (!self->extra) create_extra(...)` guard in the `extra` accessors isn't atomic. Two threads first-touching a shared `Element` both take the `!self->extra` branch and both run `create_extra`, which does `self->extra = PyMem_Malloc(...)` (`_elementtree.c:274`) with no critical section — so one allocation is overwritten (leaked) and readers can observe a torn pointer.

Minimal deterministic reproducer (exit 66 under TSan; `create_extra:274` as the write, reached via `element_attrib_getter` / `element_length`):

```python
import threading
import xml.etree.ElementTree as ET

NTHREADS = 8
barrier = threading.Barrier(NTHREADS)

def worker(elem):
    barrier.wait()
    for _ in range(4000):
        _ = elem.attrib          # if (!self->extra) create_extra(...) -- unlocked lazy init
        _ = len(elem)            # element_length reads self->extra

for _ in range(200):
    shared = ET.Element("tag")   # extra == NULL until first attrib/child touch
    ts = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
```

Confirmed still present on current `main` (3.16.0a0), on both a debug and a release `--with-thread-sanitizer` build. PR #149918's approach — taking `Py_BEGIN_CRITICAL_SECTION(self)` around the `if (!self->extra) create_extra(...)` check-and-create and the other `extra` accessors — covers this (both the read-deref and the write/write faces). Faces the fuzzer also hit: `create_extra | element_length` and `clear_extra | create_extra`.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → gh-144356 (or on PR #144357) — set iterator, the shared-*iterator* face

`fusil --tsan` independently hit this, via a **different trigger** than the report's `__length_hint__`-vs-set-mutation script: sharing a single **set iterator** across threads (no set mutation at all). `setiter_iternext` reads `si->si_pos` inside `Py_BEGIN_CRITICAL_SECTION(so)` (`setobject.c:1117`) but writes it back **outside** the section (`si->si_pos = i+1`, `:1128`), and `si->len--` is likewise unsynchronized — so two threads advancing the *same* iterator race on its private cursor. `setiter_len` reading `si->len` races the same write. The section is keyed on the set `so`, so it doesn't serialize concurrent use of one iterator.

```python
import operator, threading

NTHREADS = 8
barrier = threading.Barrier(NTHREADS)

def worker(it):
    barrier.wait()
    for _ in range(20000):
        try:
            next(it)                    # setiter_iternext: si->si_pos / si->len advance
        except StopIteration:
            pass
        operator.length_hint(it, 0)     # setiter_len: reads si->len

for _ in range(300):
    shared = iter(set(range(4096)))     # ONE shared iterator, no set mutation
    ts = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
```

Exit 66 under TSan; `setiter_iternext:1117` (read `si->si_pos`) vs `:1128` (write), plus the `setiter_len | setiter_iternext` (`si->len`) face. This is exactly the "concurrent use of the same iterator" case PR #144357 addresses by switching to `Py_BEGIN_CRITICAL_SECTION2(self, so)` and moving the cursor writes inside the section — so it's a useful corroboration of that expanded scope. Confirmed on current `main` (3.16.0a0), debug + release TSan builds.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → gh-150791 (or on PR #150792) — groupby, a simpler reproducer

Independent confirmation via `fusil --tsan`, with a reproducer that doesn't need a custom key type — just several threads consuming one shared `groupby`:

```python
import itertools, threading

NTHREADS = 8
barrier = threading.Barrier(NTHREADS)

def worker(gb):
    barrier.wait()
    try:
        list(gb)                              # groupby_next on the shared gb
    except (RuntimeError, StopIteration, ValueError, TypeError):
        pass

for _ in range(500):
    shared = itertools.groupby(range(8192))   # ONE shared groupby
    ts = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
```

Exit 66 under TSAN; the script above races `_grouper_create` against `groupby_next` (creating the child grouper while another thread advances the parent), alongside the `groupby_next`-vs-`groupby_next` race on `currgrouper` described in the issue. Also iterating the yielded sub-groups races `_grouper_next` against the parent's `currkey`/`currvalue` — consistent with PR #150792 guarding `_grouper_next` (via `Py_BEGIN_CRITICAL_SECTION(parent)`) as well as `groupby_next`. Confirmed on current `main` (3.16.0a0), debug + release TSan builds.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → NEW ISSUE (TSAN-0045) — GenericAlias iterator crash

**Title:** `segfault in ga_iternext: sharing a types.GenericAlias iterator across threads double-frees under free-threading`

**Body:**

# Bug report

### Bug description

On a free-threaded (`--disable-gil`) build, iterating a single, shared `types.GenericAlias` iterator (e.g. `iter(list[int])`) from multiple threads segfaults. `ga_iternext` (`Objects/genericaliasobject.c`) is a one-shot iterator with no synchronization:

```c
static PyObject *
ga_iternext(PyObject *op)
{
    gaiterobject *gi = (gaiterobject *)op;
    if (gi->obj == NULL) {                               // read gi->obj
        PyErr_SetNone(PyExc_StopIteration);
        return NULL;
    }
    gaobject *alias = (gaobject *)gi->obj;
    PyObject *starred_alias = Py_GenericAlias(alias->origin, alias->args);   // use gi->obj
    if (starred_alias == NULL)
        return NULL;
    ((gaobject *)starred_alias)->starred = true;
    Py_SETREF(gi->obj, NULL);                            // Py_DECREF(gi->obj); gi->obj = NULL
    return starred_alias;
}
```

Two threads both observe `gi->obj != NULL`, both build the starred alias from it, and both reach `Py_SETREF(gi->obj, NULL)` (which is `tmp = gi->obj; gi->obj = NULL; Py_DECREF(tmp)`). With `gi->obj`'s refcount at 1 (the iterator is its only holder), the two `Py_DECREF`s free it twice — a double-free / refcount underflow — and the reads of `alias->origin`/`alias->args` become a use-after-free once the other thread frees it. The process crashes.

### Reproducer

```python
import threading

NT = 16

def worker(it, barrier):
    barrier.wait()
    try:
        next(it)
    except StopIteration:
        pass

for _round in range(20000):
    shared = iter(list[int])      # ONE shared GenericAlias iterator; gi->obj refcount == 1
    bar = threading.Barrier(NT)
    threads = [threading.Thread(target=worker, args=(shared, bar)) for _ in range(NT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
```

`PYTHON_GIL=0 ./python repro.py` segfaults within the first few rounds — 5/5 runs on both a debug and a release (`-O0`, no sanitizer) free-threaded build, so it is neither debug-only nor sanitizer-only:

```
Thread NNNN received signal SIGSEGV, Segmentation fault.
#0  ga_iternext (op=...) at Objects/genericaliasobject.c:952        (Py_SETREF(gi->obj, NULL))
#1  builtin_next                Python/bltinmodule.c:1776
#2  _Py_BuiltinCallFast_StackRef Python/ceval.c:817
#3  _PyEval_EvalFrameDefault     Python/generated_cases.c.h:2510    (next(it))
...
```

ThreadSanitizer reports the same as a data race on `gi->obj` (`WARNING: ThreadSanitizer: data race... in ga_iternext`, exit 66).

### Suggested fix

Consume `gi->obj` atomically so exactly one thread takes it:

```c
PyObject *obj = _Py_atomic_exchange_ptr(&gi->obj, NULL);
if (obj == NULL) {
    PyErr_SetNone(PyExc_StopIteration);
    return NULL;
}
gaobject *alias = (gaobject *)obj;
PyObject *starred_alias = Py_GenericAlias(alias->origin, alias->args);
if (starred_alias == NULL) {
    Py_DECREF(obj);
    return NULL;
}
((gaobject *)starred_alias)->starred = true;
Py_DECREF(obj);
return starred_alias;
```

(Or wrap the body in `Py_BEGIN_CRITICAL_SECTION(gi)`.) This matches the "consume once" semantics and keeps the GIL build unchanged.

Per the iterator free-threading strategy (gh-124397), concurrent iteration may return duplicate or skipped values or raise — but it must not crash; this crashes. Distinct from gh-153298, which is the `GenericAlias.__parameters__` lazy-init race, not the iterator.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; crash confirmed by re-running the reproducer without a sanitizer on a plain free-threaded build. Draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → NEW ISSUE (TSAN-0043) — descriptor __qualname__ lazy cache

**Title:** `Data race + leak: descr_get_qualname lazily caches d_qualname without synchronization (free-threading)`

**Body:**

# Bug report

### Bug description

On a free-threaded (`--disable-gil`) build, reading `__qualname__` on the **same** descriptor from
multiple threads is a data race on the lazily-populated cache `descr->d_qualname`.
`descr_get_qualname` (`Objects/descrobject.c`) does an unsynchronized check-then-write:

```c
static PyObject *
descr_get_qualname(PyObject *self, void *Py_UNUSED(ignored))
{
    PyDescrObject *descr = (PyDescrObject *)self;
    if (descr->d_qualname == NULL)
        descr->d_qualname = calculate_qualname(descr);   // WRITE, no lock
    return Py_XNewRef(descr->d_qualname);
}
```

Descriptors (`method_descriptor` / `getset_descriptor` / `wrapper_descriptor`) live on their owning
type, so they are shared across all threads. When two threads first read the same descriptor's
`__qualname__`, both observe `d_qualname == NULL`, both call `calculate_qualname`, and both store
into `descr->d_qualname` — a write/write data race on the pointer, plus a **leak**: the store is a
plain assignment (the old value is `NULL`), so the losing thread's freshly-computed str is
overwritten and never freed.

This is value-benign (the two computed qualnames are equal, and there is no double-free, so it does
not crash), but it is a genuine C11 data race and a small leak on a shared object. It is the same
lazy-cache-without-synchronization pattern that gh-125267 fixed for `object.__reduce_ex__`'s
`objreduce` cache.

### Reproducer

```python
import threading

NT = 8

# Builtin C descriptors keep d_qualname == NULL until __qualname__ is first read, so each can be
# raced exactly once. Race __qualname__ across threads on each freshly-untouched descriptor.
descrs = []
for tp in (str, bytes, list, dict, set, int, float, tuple, frozenset, bytearray):
    for name, v in vars(tp).items():
        if type(v).__name__ in ("method_descriptor", "getset_descriptor", "wrapper_descriptor"):
            descrs.append(v)


def worker(descriptor, barrier):
    barrier.wait()
    for _ in range(20):
        _ = descriptor.__qualname__


for _round in range(50):
    for d in descrs:
        bar = threading.Barrier(NT)
        threads = [threading.Thread(target=worker, args=(d, bar)) for _ in range(NT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
print("done")
```

Under a `--with-thread-sanitizer` free-threaded build (`PYTHON_GIL=0`, `TSAN_OPTIONS=…exitcode=66…`)
this reports `WARNING: ThreadSanitizer: data race … in descr_get_qualname` (both sides
`descr_get_qualname`, on `descr->d_qualname`) deterministically. Reproduced on both a debug and a
release TSan build.

### Suggested fix

Serialize the lazy init — either a per-object critical section:

```c
if (descr->d_qualname == NULL) {
    Py_BEGIN_CRITICAL_SECTION(descr);
    if (descr->d_qualname == NULL)                       // re-check
        descr->d_qualname = calculate_qualname(descr);
    Py_END_CRITICAL_SECTION();
}
return Py_XNewRef(descr->d_qualname);
```

or a one-shot atomic compare-exchange (compute, `CAS(&descr->d_qualname, NULL, new)`, `Py_DECREF`
the loser). The GIL build is unchanged either way. (gh-125267 took the "initialize eagerly" route
for the analogous `objreduce` cache.)

### CPython versions tested on

CPython `main` (3.16.0a0), free-threaded `--disable-gil` build.

### Operating systems tested on

Linux

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer. Draft and reproducer by Claude Code, minimized and reviewed by hand.)*
