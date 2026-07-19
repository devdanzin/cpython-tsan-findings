# Crash (SIGSEGV): a shared `types.GenericAlias` iterator double-DECREFs `gi->obj` under free-threading (`ga_iternext`, `Objects/genericaliasobject.c:952`)

*The GenericAlias iterator (`gaiterobject`, from `iter(list[int])`) is one-shot: `ga_iternext` reads `gi->obj`, builds one starred alias from it, then does `Py_SETREF(gi->obj, NULL)` (`:952`) — with no synchronization. When a single such iterator is shared across threads, two threads both read `gi->obj != NULL`, both use it, and both `Py_SETREF(gi->obj, NULL)` → a data race on `gi->obj` and a **double-DECREF / use-after-free** of the object it pointed to. This is not just a TSan warning: it **segfaults deterministically** on a plain free-threaded build.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Objects/genericaliasobject.c`:

```c
static PyObject *
ga_iternext(PyObject *op)
{
    gaiterobject *gi = (gaiterobject *)op;
    if (gi->obj == NULL) {                                        /* read gi->obj */
        PyErr_SetNone(PyExc_StopIteration);
        return NULL;
    }
    gaobject *alias = (gaobject *)gi->obj;
    PyObject *starred_alias = Py_GenericAlias(alias->origin, alias->args);  /* uses gi->obj */
    if (starred_alias == NULL)
        return NULL;
    ((gaobject *)starred_alias)->starred = true;
    Py_SETREF(gi->obj, NULL);                                    /* :952  Py_DECREF(gi->obj); gi->obj = NULL */
    return starred_alias;
}
```

`ga_iternext` has no critical section. Two threads sharing one `gaiterobject` both pass the `gi->obj == NULL` check (both see it non-NULL), both dereference `alias = gi->obj` to build the starred alias, and both reach `Py_SETREF(gi->obj, NULL)`. `Py_SETREF` expands to `tmp = gi->obj; gi->obj = NULL; Py_DECREF(tmp)`. With `gi->obj`'s refcount at 1 (the iterator is its only holder), the two DECREFs drop it to 0 then underflow — a double-free — and the second thread's earlier `alias->origin`/`alias->args` reads become a use-after-free once the first thread frees it. The result is a hard crash.

## Reproducer

```python
import threading

# A shared GenericAlias iterator (iter(list[int]) -> gaiterobject) is one-shot: ga_iternext reads
# gi->obj and does Py_SETREF(gi->obj, NULL) (Objects/genericaliasobject.c:952) with no lock. Two
# threads both reaching it race gi->obj and double-DECREF the old referent -> refcount underflow /
# use-after-free. Under TSan: exit 66. On a plain free-threaded build (no TSan): SIGSEGV at
# ga_iternext, deterministically and near-instantly (crashes within the first few rounds).
NT = 16


def worker(it, barrier):
    barrier.wait()
    try:
        next(it)  # ga_iternext: Py_SETREF(gi->obj, NULL)
    except StopIteration:
        pass


for _round in range(20000):
    shared = iter(list[int])  # ONE shared gaiterobject; gi->obj refcount == 1
    bar = threading.Barrier(NT)
    threads = [threading.Thread(target=worker, args=(shared, bar)) for _ in range(NT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
```

Two outcomes, both deterministic:

- **Plain free-threaded build (no sanitizer) → SIGSEGV.** `PYTHON_GIL=0 ./python repro.py` crashes with exit 139 within the first few rounds, **5/5 runs** on both `debug-ft-nojit` and `release-ft-nojit-o0` (so it is neither debug-only nor sanitizer-only).
- **TSan build → exit 66** (`WARNING: ThreadSanitizer: data race … in ga_iternext`).

## Crash backtrace (`debug-ft-nojit` / `release-ft-nojit-o0`, `PYTHON_GIL=0`)

```
Thread NNNN received signal SIGSEGV, Segmentation fault.
#0  ga_iternext (op=0x...) at Objects/genericaliasobject.c:952        (Py_SETREF(gi->obj, NULL))
#1  builtin_next                Python/bltinmodule.c:1776
#2  _Py_BuiltinCallFast_StackRef Python/ceval.c:817
#3  _PyEval_EvalFrameDefault     Python/generated_cases.c.h:2510      (next(it))
...
```

(Full backtrace in `crash_backtrace.txt`; TSan report in `tsan_report.txt`.)

## Root cause

A one-shot iterator whose single act — consume `gi->obj`, then clear it with `Py_SETREF` — is a read-modify-write on a shared `PyObject*` with no per-object locking. Safe under the GIL (only one thread runs `ga_iternext` at a time); in the free-threaded build a shared iterator lets two threads both consume the same reference, double-freeing it. `Py_SETREF` is not atomic, and neither is the `gi->obj == NULL` guard.

## Impact / severity

**High (memory-unsafe, crashes).** A hard SIGSEGV / double-free reachable from pure Python on a free-threaded build. CPython's iterator free-threading strategy ([gh-124397](https://github.com/python/cpython/issues/124397), Raymond Hettinger) sets the bar explicitly — concurrent iteration "is allowed to return duplicate values, skip values, or raise an exception," but it **must not crash**. This crashes, so it is squarely in scope. The mitigating factor is likelihood: sharing a *one-shot* `iter(list[int])` across threads is unusual, so real-world exposure is low — but the failure mode is a crash, not a benign wrong value.

## Suggested fix

Consume `gi->obj` atomically so exactly one thread gets it:

```c
PyObject *obj = _Py_atomic_exchange_ptr(&gi->obj, NULL);
if (obj == NULL) { PyErr_SetNone(PyExc_StopIteration); return NULL; }
gaobject *alias = (gaobject *)obj;
PyObject *starred_alias = Py_GenericAlias(alias->origin, alias->args);
Py_DECREF(obj);
... 
```

or take the iterator's per-object critical section (`Py_BEGIN_CRITICAL_SECTION(gi)`) around the whole body. The atomic-exchange form is cheaper and matches the "consume once" semantics.

## Notes

- **Appears genuinely unfiled.** A `gh api` tracker search for `ga_iternext` / GenericAlias-iterator-crash returned nothing. gh-153298 ("Data race creating `types.GenericAlias.__parameters__` lazily") is a *different* GenericAlias FT race (the `__parameters__` lazy-init on the alias, not the iterator). **Fileable — a crashing free-threading bug.** Outward-facing; awaiting maintainer go-ahead.
- Same "shared one-shot iterator exhaustion → double-DECREF" shape as the bytes/str `it_seq` faces (TSAN-0037/0038), but here it reliably crashes rather than just tripping TSan.
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 11) — surfaced only because `--tsan-no-halt` captured races past the first per session; the crash was then confirmed by re-running the reproducer without a sanitizer on a plain free-threaded build.

---

*New crashing free-threading bug (SIGSEGV). Not yet filed.*
