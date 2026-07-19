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

---

## → gh-153981 (corroboration) — count slow-mode UAF + endorse PR #153983

Confirming this independently. `fusil --tsan` (a ThreadSanitizer fuzzer) surfaced the same slow-mode use-after-free, which I'd reported as a follow-up on the fast-mode issue (gh-153908) just before being pointed at this issue and your PR.

The mechanism matches: in slow (big-int) mode `count_repr` borrows `lz->long_cnt` and `repr()`s it (the `if (lz->long_cnt == NULL)` mode check + `PyUnicode_FromFormat("%R", …, lz->long_cnt)`) with no synchronization, while `count_nextlong` does `lz->long_cnt = stepped_up` under `Py_BEGIN_CRITICAL_SECTION(lz)` and returns the old value — which the `next()` caller then DECREFs, so `count_repr` can `repr()` a freed object. TSan flags it as a data race on `lz->long_cnt` (`count_nextlong` vs `count_repr`), and when the free wins it's a `SEGV` in `PyObject_Repr` (matching your `SEGV object.c:766`). Reproduced deterministically on a free-threaded `--with-thread-sanitizer` build of current `main`; fast-mode `count()` is clean.

Repro:

```python
import itertools, threading
NT = 8
for _ in range(3000):
    it = itertools.count(10**18, 2)          # big-int -> slow (long_cnt) mode
    bar = threading.Barrier(NT)
    def work(advance, it=it, bar=bar):
        bar.wait()
        for _ in range(400):
            next(it) if advance else repr(it)
    ts = [threading.Thread(target=work, args=(i % 2 == 0,)) for i in range(NT)]
    for t in ts: t.start()
    for t in ts: t.join()
```

PR #153983 looks like the correct fix: snapshotting `long_cnt`/`long_step` with `Py_XNewRef` inside `Py_BEGIN_CRITICAL_SECTION(lz)` and formatting from the locals addresses both halves — the data race (the reads are now under the same critical section `count_nextlong` writes under) and the use-after-free (the strong reference keeps `long_cnt` alive across the `repr()`). A bare atomic load of the pointer would have closed the race but not the UAF, so the strong-ref snapshot is the key part.

*(Found by fusil --tsan, a ThreadSanitizer fuzzer; comment + reproducer by Claude Code, reviewed by hand.)*

---

## → NEW ISSUE (TSAN-0053) — dict iterator double-free crash (crash face of closed gh-148873)

**Title:** `Sharing a dict iterator across threads double-DECREFs di_dict under free-threading`

**Body:**

# Bug report

### Bug description

On a free-threaded build, advancing a single, shared `dict` iterator (`iter({...})`) from multiple threads double-frees the underlying dict and crashes.

All three dict iterators (`dict_keyiterator` / `dict_valueiterator` / `dict_itemiterator`) route `next()` through `dictiter_iternext_threadsafe` (`Objects/dictobject.c`). Its exhaustion path drops the iterator's owning reference to the dict:

```c
fail:
    di->di_dict = NULL;   /* non-atomic clear */
    Py_DECREF(d);         /* drop the iterator's ONE owning ref to the dict */
    return -1;
```

and the caller reads that reference with no lock:

```c
static PyObject*
dictiter_iternextkey(PyObject *self)
{
    dictiterobject *di = (dictiterobject *)self;
    PyDictObject *d = di->di_dict;      /* plain read */
    if (d == NULL)
        return NULL;
    PyObject *value;
    if (dictiter_iternext_threadsafe(d, self, &value, NULL) < 0) {
        value = NULL;
    }
    return value;
}
```

The iterator holds exactly one reference to the dict (`di_dict`). Two threads calling `next()` on the same near-exhausted iterator interleave as: both read `d = di->di_dict` (non-NULL), both reach `fail:`, and both run `Py_DECREF(d)`. The second `Py_DECREF` has no matching reference — the dict's refcount underflows / it is freed one owner too early, and a sibling thread still walking `d` (and the dict freelist / keys object) then touches freed memory.

This `fail: di->di_dict = NULL; Py_DECREF(d)` pattern predates free-threading (it is correct under the GIL, where only one thread runs the iterator); the lock-free `dictiter_iternext_threadsafe` wrapper carried it over unguarded.

### Reproducer

```python
import threading

NT = 8
ITERS = 200_000

def newit():
    return iter({k: k for k in range(32)})

cell = [newit()]

def worker():
    for _ in range(ITERS):
        it = cell[0]
        try:
            next(it)                 # dictiter_iternextkey -> dictiter_iternext_threadsafe
        except StopIteration:
            cell[0] = newit()        # refill so the fail: (exhaustion) path is hit repeatedly
        except Exception:
            pass

threads = [threading.Thread(target=worker) for _ in range(NT)]
for t in threads: t.start()
for t in threads: t.join()
print("done, no crash")
```

`PYTHON_GIL=0 ./python repro.py` on a free-threaded build:

- **Debug build → SIGABRT within seconds, ~8/8 runs**, with `_Py_NegativeRefcount` on the dict (`Objects/dictobject.c:6159`), plus downstream corruption faces as the freed dict/keys object is reused (`dictkeys_incref` immortal-refcount assert `:484`; `new_dict` type assert `:978`; `clear_freelist` `Objects/object.c:909`; `validate_refcounts` in `gc_free_threading.c`).
- **Release build (`-O0`, no sanitizer) → SIGSEGV** (use-after-free), or occasionally `Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked` from the corrupted dict mutex.

So it is neither debug-only nor sanitizer-only. Debug backtrace (the negative-refcount object **is** the dict `d`):

```
#8  _PyObject_AssertFailed (obj=0x...dict...) at Objects/object.c:3278
#9  _Py_NegativeRefcount               at Objects/object.c:275
#12 Py_DECREF                          at ./Include/refcount.h:363
#13 dictiter_iternext_threadsafe (d=0x...dict...) at Objects/dictobject.c:6159    <-- fail: Py_DECREF(d)
#14 dictiter_iternextkey                at Objects/dictobject.c:5791
#15 builtin_next                        at Python/bltinmodule.c:1776
```

### Suggested fix

Consume the reference atomically so exactly one thread performs the DECREF:

```c
fail:
    PyDictObject *old = _Py_atomic_exchange_ptr(&di->di_dict, NULL);
    if (old != NULL) {
        Py_DECREF(old);
    }
    return -1;
```

and keep the dict alive for the duration of the lock-free walk (the caller uses `d` and its keys/values across the whole `dictiter_iternext_threadsafe` body) so a sibling that wins the exchange cannot free it mid-iteration — take a strong reference or the dict's critical section for the walk. One fix covers keys/values/items, since all three route through `dictiter_iternext_threadsafe`.

### Why this is not the documented value-benign iterator race

This function was written to be shared across threads, and a crash is explicitly out of contract:

- The lock-free dict iterator `dictiter_iternext_threadsafe` was added by **gh-112075 / PR #115108** ("Iterating a dict shouldn't require locks"), under the umbrella **gh-112075** ("Make `dict` objects thread-safe in `--disable-gil` builds"). PR #115108 states it "[handles] races against the dict as well as **allowing the iterator to be used from multiple threads simultaneously**." It made the *value read* safe (`_Py_TryIncrefCompare` / `acquire_key_value`) but carried the old `fail: di->di_dict = NULL; Py_DECREF(d)` exhaustion path in unchanged — hence this double-free.
- **gh-120496** ("Sequence iterator thread-safety") decided *not* to fix the fact that concurrent iteration can return duplicate/skipped values, and documented it instead (only the `Doc/glossary.rst` note, PR #120685, merged; the code-fix PRs were closed). But the contract agreed on that issue is explicit — @colesbury and @eendebakpt: iterating from multiple threads "**will not crash the interpreter**" (that's the acceptable line; wrong values are OK, crashes are not), and @eendebakpt flagged "the risks of **overflows inside the C code**."
- **gh-148873** reported this iterator's data-race face and was **closed as a duplicate of gh-120496** — folding a double-free into the value-benign class. That is the gap this issue closes: the same unsynchronized clear-and-DECREF is not benign — it double-frees the dict and crashes (negative refcount / UAF / SIGSEGV) on plain free-threaded builds, which gh-120496 / gh-124397 put squarely on the not-acceptable side.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; crash confirmed by re-running the reproducer without a sanitizer on a plain free-threaded build. Draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → gh-144356 / PR gh-144357 (corroboration) — the set-iterator race is a memory-safety CRASH; the stalled PR fixes it

This isn't only a `__length_hint__` data race — the same `setiter_iternext` path **double-frees the set** and crashes. On a free-threaded build, `setiter_iternext` (`Objects/setobject.c`) reads `so = si->si_set` unguarded, takes only the *set's* critical section around the table scan, and then on exhaustion runs — **outside** that section:

```c
    if (key == NULL) {
        si->si_set = NULL;      /* :1130 */
        Py_DECREF(so);          /* :1131  drop the iterator's one owning ref to the set */
        return NULL;
    }
```

Two threads advancing the **same** set iterator to exhaustion both read the same non-NULL `so` and both `Py_DECREF(so)` → the set's refcount underflows → use-after-free.

Reproducer:

```python
import threading

NT = 8
def newit():
    return iter(set(range(32)))
cell = [newit()]
def worker():
    for _ in range(200_000):
        it = cell[0]
        try:
            next(it)
        except StopIteration:
            cell[0] = newit()
        except Exception:
            pass
ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
```

`PYTHON_GIL=0 ./python repro.py`:
- **debug free-threaded build** → `Objects/setobject.c:1131: _Py_NegativeRefcount: object has negative ref count`, ~8/8 within seconds.
- **release `-O0` free-threaded build** (no sanitizer) → SIGSEGV / SIGABRT, core dumped, 6/6.

The realistic form uses a **long-lived shared `frozenset`** (e.g. a module-level constant, refcount > 1): the underflow is silent until the next `gc.collect()` reports `Python/gc_free_threading.c:999 update_refs: Assertion "refcount >= 0" failed` on a `frozenset` with a wild refcount (~2^60), or a later access UAFs.

**PR #144357 fixes this** — widening to `Py_BEGIN_CRITICAL_SECTION2(self, so)` and, under `Py_GIL_DISABLED`, making exhaustion sticky via `si_pos = -1` and removing the `si_set = NULL; Py_DECREF(so)` from `iternext` (dropping the set ref only in dealloc) is exactly what closes the double-DECREF. It'd be good to land it — this is a memory-safety bug, not just a value race. (This is the set sibling of the dict-iterator double-free just filed as gh-154130.)

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; crash confirmed by re-running the reproducer without a sanitizer on plain free-threaded builds. Comment + reproducer by Claude Code, reviewed by hand.)*

---

## → gh-154130 (corroboration) — the dict-iterator double-free also hits LONG-LIVED shared dicts/frozendicts (the dangerous face)

Follow-up on the reproducer: the same `dictiter_iternext_threadsafe` double-DECREF is **not** limited to throwaway `iter({...})`. It hits any long-lived shared dict — and, because `iter(frozendict)` returns a `dict_keyiterator`, any shared **`frozendict`** (e.g. a module-level constant) too. On a long-lived object (refcount > 1) the double-DECREF doesn't go immediately negative — the corruption is **silent** until the next `gc.collect()` catches it:

```python
import threading, gc

fd = frozendict({4:'FREE',1:'LOCAL',3:'GLOBAL_IMPLICIT',2:'GLOBAL_EXPLICIT',5:'CELL'})
NT = 8
cell = [iter(fd)]
def worker(role):
    for i in range(100_000):
        it = cell[0]
        try:
            next(it)
        except StopIteration:
            cell[0] = iter(fd)
        except Exception:
            pass
        if role == 2 and i % 32 == 0:
            gc.collect()
ts = [threading.Thread(target=worker, args=(i % 3,)) for i in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
```

`PYTHON_GIL=0 ./python repro.py` → `Python/gc_free_threading.c:1116: validate_gc_objects: Assertion "gc_get_refs(op) >= 0" failed: refcount is too small`, object type `frozendict`, ~8/8 on a debug free-threaded build (SIGSEGV on release).

This is the more concerning form of the bug: a real program that keeps a shared module-level `dict`/`frozendict` and iterates it from several threads silently corrupts its refcount, surfacing as an unrelated GC crash. A ThreadSanitizer fuzzer (`fusil --tsan`) hit it across ~9 unrelated stdlib modules that each expose a module-level `frozendict` (symtable, functools, gettext, json, …) — the module is incidental; the shared frozendict iterator is the cause.

*(Reproducer + note by Claude Code, reviewed by hand.)*
