# Crash: a shared `dict` iterator double-DECREFs `di_dict` in its exhaustion path under free-threading (`dictiter_iternext_threadsafe`, `Objects/dictobject.c:6159`)

*The free-threaded dict iterators (`dict_keyiterator` / `dict_valueiterator` / `dict_itemiterator`) advance `next()` through `dictiter_iternext_threadsafe`, whose exhaustion path is `fail: di->di_dict = NULL; Py_DECREF(d);` — a non-atomic clear + DECREF of the iterator's single owning reference to the dict — while the caller `dictiter_iternextkey` reads `d = di->di_dict` with no lock. When one dict iterator is advanced by `next()` from several threads and they reach exhaustion together, two threads both read the same non-NULL `d` and both run the `fail:` `Py_DECREF(d)` → the dict's refcount underflows → **double-free / use-after-free**. This is not just a TSan warning: it aborts with `_Py_NegativeRefcount` on a plain debug free-threaded build and **SIGSEGVs** on a release build.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer and captured the backtrace; the maintainer reviewed and edited it._

## Summary

`Objects/dictobject.c` (main@`a1d580430c8`). The caller reads the dict reference unguarded:

```c
static PyObject*
dictiter_iternextkey(PyObject *self)
{
    dictiterobject *di = (dictiterobject *)self;
    PyDictObject *d = di->di_dict;          /* plain read of the owning ref (:5784) */
    if (d == NULL)
        return NULL;
    PyObject *value;
    if (dictiter_iternext_threadsafe(d, self, &value, NULL) < 0) {   /* :5791 */
        value = NULL;
    }
    return value;
}
```

and `dictiter_iternext_threadsafe` drops that reference on exhaustion:

```c
fail:
    di->di_dict = NULL;   /* :6158  non-atomic clear */
    Py_DECREF(d);         /* :6159  drop the iterator's ONE owning ref to the dict */
    return -1;
```

The iterator holds exactly **one** reference to the dict (`di_dict`). With no synchronization on the clear-and-decref, two threads `T1`/`T2` calling `next()` on the same near-exhausted iterator interleave as:

- `T1`: `d = di->di_dict` (non-NULL)
- `T2`: `d = di->di_dict` (same non-NULL — before `T1` clears it)
- `T1`: reaches `fail:` → `di->di_dict = NULL; Py_DECREF(d)` (refcount: N → N-1, correct)
- `T2`: reaches `fail:` → `di->di_dict = NULL` (already NULL); `Py_DECREF(d)` **again** (N-1 → N-2)

The second DECREF has no matching reference — the dict is freed one owner too early (or, in debug, its shared refcount underflows past zero). Everything downstream — a sibling thread still walking `d`, the dict freelist, the keys object — is then operating on freed/corrupt memory.

All three dict-iterator types funnel through `dictiter_iternext_threadsafe` (`dictiter_iternextkey` passes `&key,NULL`; `dictiter_iternextvalue` passes `NULL,&value`; `dictiter_iternextitem` passes `&key,&value`), so keys/values/items are all affected and one fix covers them.

## Reproducer

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

- **`debug-ft-nojit` → SIGABRT within seconds, ~8/8 runs.** `_Py_NegativeRefcount` on the dict (`Objects/dictobject.c:6159`), and the downstream corruption faces as the freed dict/keys object is reused: `dictkeys_incref` immortal-refcount assert (`:484`), `new_dict` type assert (`Py_IS_TYPE(mp, &PyDict_Type)`, `:978`), `clear_freelist` (`Objects/object.c:909`), `validate_refcounts` (`gc_free_threading.c`).
- **`release-ft-nojit-o0` (no sanitizer) → SIGSEGV** (use-after-free), or occasionally a deadlock/`Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked` from the corrupted dict mutex.

So it is neither debug-only nor sanitizer-only — a real memory-safety crash on a plain release free-threaded build.

## Crash backtrace (`debug-ft-nojit`, `PYTHON_GIL=0`, under gdb)

The negative-refcount object (`obj=0x2000382d0f0`) **is** the dict `d` passed to `dictiter_iternext_threadsafe`:

```
#8  _PyObject_AssertFailed (obj=0x2000382d0f0, ...) at Objects/object.c:3278
#9  _Py_NegativeRefcount (op=0x2000382d0f0) at Objects/object.c:275
#10 _Py_DecRefSharedIsDead (o=0x2000382d0f0) at Objects/object.c:403
#12 Py_DECREF (op=0x2000382d0f0) at ./Include/refcount.h:363
#13 dictiter_iternext_threadsafe (d=0x2000382d0f0, self=0x2002c1301d0, ...) at Objects/dictobject.c:6159   <-- fail: Py_DECREF(d)
#14 dictiter_iternextkey (self=0x2002c1301d0)                          at Objects/dictobject.c:5791
#15 builtin_next                                                      at Python/bltinmodule.c:1776
#16 _Py_BuiltinCallFast_StackRef                                      at Python/ceval.c:817
```

(Full backtrace in `crash_backtrace.txt`.)

## Root cause

The `fail:` path — `di->di_dict = NULL; Py_DECREF(d)` — was written for GIL-era Python (`git blame`: 2004 / 2010 / 2016), where only one thread ever runs `dictiter_iternext*` at a time, so clearing and dropping the reference is trivially safe. Free-threading added the lock-free `dictiter_iternext_threadsafe` fast path but carried this exhaustion path over unchanged. Under `--disable-gil` a shared iterator lets two threads reach `fail:` concurrently and both drop the single owning reference — a read-modify-write on a shared `PyObject *` (`di_dict`) and its referent's refcount with no per-object locking.

## Impact / severity

**High (memory-unsafe, crashes).** A double-free / use-after-free of a live `dict`, reachable from pure Python on a free-threaded build. CPython's iterator free-threading strategy ([gh-124397](https://github.com/python/cpython/issues/124397), Raymond Hettinger) sets the bar explicitly — concurrent iteration "is allowed to return duplicate values, skip values, or raise an exception," but it **must not crash**. This crashes, so it is in scope. The mitigating factor is likelihood: sharing one dict *iterator* across threads is unusual (a dict is normally iterated once, in one thread). But the failure mode is a hard crash, not a benign wrong value.

## Suggested fix

Consume the reference atomically so exactly one thread performs the DECREF:

```c
fail:
    PyDictObject *old = _Py_atomic_exchange_ptr(&di->di_dict, NULL);
    if (old != NULL) {
        Py_DECREF(old);
    }
    return -1;
```

and keep the dict alive for the duration of the lock-free walk — the caller reads `d = di->di_dict` and uses it (and its keys/values) across the whole `dictiter_iternext_threadsafe` body, so a sibling that wins the exchange must not free it mid-iteration; take a strong reference (try-incref) or the dict's critical section for the walk. One fix covers keys/values/items (all route through `dictiter_iternext_threadsafe`). Same "consume once" shape as the fix suggested for the GenericAlias iterator (TSAN-0045 / cpython#154043).

## Notes

- **Prior art — the data-race face was reported and closed unfixed.** [cpython#148873](https://github.com/python/cpython/issues/148873) ("Possible data race in `dictiter_iternext` with free-threading build", **closed 2026-04-22**, no fix commit, no comments) reported the *same* `di_dict` race with the *same* reproducer shape (a shared `iter(d)` driven by two `next()` threads) and the *same* call stack, pointing at the `di->di_dict = NULL` write racing the read. It was closed without a fix and current `main` still double-frees. This report escalates that from a value-benign-looking data race to a demonstrated **crash** (negative refcount / UAF / SIGSEGV) with a reliable pure-Python reproducer — the same escalation that made the GenericAlias iterator ([cpython#154043](https://github.com/python/cpython/issues/154043)) and the struct `iter_unpack` iterator ([cpython#154013](https://github.com/python/cpython/issues/154013)) fileable. A fresh issue framed as a memory-safety crash, cross-referencing #148873, is the right move.
- **Distinct from our own TSAN-0026** (the `ma_values` plain-vs-atomic *data race* in the same function) — that is a benign incomplete-atomic read; this is the `di_dict` double-free crash.
- **Found via the `--tsan` un-masking profile (fleet-15).** The plain dict-iterator data race is a suppressed gateway (`gateway_suppressions.txt`: `race:dictiter_iternext`); with it suppressed, the rarer double-DECREF **crash** — an abort/segfault, not a TSan data-race report, hence the `tsanNOPARSE` label — surfaced instead. The fuzzed target module (`_colorize`) is incidental: the racing object was a plain `iter({...})` in the fuzzer's shared-iterator op.

---

*New crashing free-threading bug (double-free / UAF). The data-race face (cpython#148873) was closed without a fix; this is the memory-safety escalation. Not yet filed.*
