# Data race: a shared `itertools.groupby` iterator mutates `currkey`/`currvalue`/`currgrouper` with no per-object lock (`groupby_next`, `Modules/itertoolsmodule.c`)

*`itertools.groupby` keeps its cross-call state — `currkey`, `currvalue`, `tgtkey`, and the current child grouper `currgrouper` — in plain fields on the `groupbyobject`, with **no** critical section anywhere in `groupby_next`. When one `groupby` iterator is shared across threads (e.g. several workers each doing `list(gb)`), `groupby_next` writes `gbo->currgrouper` and (via `groupby_step`) swaps `gbo->currvalue`/`gbo->currkey` while another thread's `groupby_next`/`_grouper_next` reads or writes the same fields — a data race on the group cursor, with unsynchronized `Py_XSETREF`/`Py_XDECREF` on shared `PyObject*` fields.*

**This is not a new find:** it is [gh-150791](https://github.com/python/cpython/issues/150791) ("`groupby_next` data race on free-threaded builds"), with the open (unmerged) fix [PR #150792](https://github.com/python/cpython/pull/150792) ("gh-150791: add critical section for `groupby.next`"). The issue describes exactly this race — two threads in `groupby_next` racing `gbo->currgrouper` (one writing `NULL` at `:537`, the other storing the new grouper via `_grouper_create` at `:633`), corrupting state and raising `AttributeError` on slot accesses of live objects. The earlier, *merged* groupby fixes ([gh-143543](https://github.com/python/cpython/issues/143543) / [gh-146613](https://github.com/python/cpython/issues/146613)) are **re-entrancy** UAF fixes (a user `__eq__` re-entering `next()`), orthogonal to the thread race — they add `Py_INCREF` snapshots but no locking. `fusil --tsan` (fleet 10) reproduced the thread race independently.

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`groupbyobject` (`Modules/itertoolsmodule.c:427`) holds:

```c
typedef struct {
    PyObject_HEAD
    Py_ssize_t tgtkeyoffset;
    PyObject *it;
    PyObject *tgtkey;
    PyObject *currkey;
    PyObject *currvalue;
    const void *currgrouper;  /* borrowed reference */
} groupbyobject;
```

`groupby_next` (`:531`) has **no** `Py_BEGIN_CRITICAL_SECTION`. It plain-writes `currgrouper` and calls `groupby_step`, which read-modify-writes the shared key/value fields:

```c
static PyObject *
groupby_next(PyObject *op)
{
    groupbyobject *gbo = groupbyobject_CAST(op);
    gbo->currgrouper = NULL;                 /* :537  PLAIN write, no lock */
    for (;;) {
        ...
        if (groupby_step(gbo) < 0) ...       /* mutates currkey/currvalue */
    }
    ...
}

Py_LOCAL_INLINE(int)
groupby_step(groupbyobject *gbo)
{
    ...
    oldvalue = gbo->currvalue;
    gbo->currvalue = newvalue;               /* PLAIN write */
    Py_XSETREF(gbo->currkey, newkey);        /* PLAIN read-modify-write + DECREF */
    Py_XDECREF(oldvalue);
    return 0;
}
```

Two threads driving the same shared `groupby` race on `currgrouper`/`currkey`/`currvalue`. Besides the value race, the unsynchronized `Py_XSETREF`/`Py_XDECREF` on shared `PyObject*` fields is a refcount hazard (lost decref / double-decref) under concurrency. TSan reports `groupby_next` vs `groupby_next` (and `_grouper_create`/`_grouper_next` when the child grouper is involved).

## Reproducer

```python
import itertools
import threading

# A shared groupby iterator: several threads drive groupby_next concurrently (via list()),
# racing gbo->currkey / currvalue / currgrouper (advanced with no per-object lock).
NTHREADS = 8
barrier = threading.Barrier(NTHREADS)


def worker(gb):
    barrier.wait()
    try:
        list(gb)  # list_extend -> groupby_next on the shared gb
    except (RuntimeError, StopIteration, ValueError, TypeError):
        pass


for _ in range(500):
    shared = itertools.groupby(range(8192))  # ONE shared groupby
    threads = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
print("done")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:exitcode=66:history_size=4" \
  setarch -R ./python repro.py
```

Deterministic, exit **66**. Reproduces on **both** `debug-ft-nojit-tsan` and `release-ft-nojit-tsan`.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, fleet build `a1d580430c8`)

```
WARNING: ThreadSanitizer: data race
  Write of size 8 by thread T3:
    #0 groupby_next               Modules/itertoolsmodule.c:537   (gbo->currgrouper = NULL / currvalue swap)
    #1 list_extend_iter_lock_held Objects/listobject.c:1318
    ...                           (list(gb))

  Previous write of size 8 by thread T1:
    #0 groupby_next               Modules/itertoolsmodule.c:537
    ...
SUMMARY: ThreadSanitizer: data race Modules/itertoolsmodule.c in groupby_next
```

Fleet-10 drove the `groupby_next | groupby_next` self-race (1 vehicle); the isolated repro also surfaces `_grouper_create | groupby_next` and `_grouper_create | _grouper_next` (the child-grouper faces). (Full report in `tsan_report.txt`.)

## Root cause

`groupby` maintains a small state machine across `next()` calls — the target key `tgtkey`, the current `currkey`/`currvalue`, and the currently-active child grouper `currgrouper` — entirely in plain object fields, and `groupby_next`/`groupby_step` mutate them with no per-object critical section. This is fine under the GIL (one thread advances at a time) but is a data race in the free-threaded build when a single `groupby` is shared. The existing groupby hardening is orthogonal: gh-143543 (and its sibling gh-146613 for `_grouper_next`) fixed a **re-entrancy** use-after-free where a user-defined `__eq__`, invoked by `PyObject_RichCompareBool(tgtkey, currkey)`, re-enters `next()` and frees the objects mid-compare; the fix takes `Py_INCREF` snapshots around the compare. That protects a *single* thread against re-entrancy — it adds no locking and does nothing for two threads.

## Impact / severity

**Moderate.** Unlike a purely value-benign cursor race, this one *corrupts* the iterator's internal state: gh-150791 reports it producing `AttributeError` on slot accesses of live objects, and the unsynchronized `Py_XSETREF`/`Py_XDECREF` on `currkey`/`currvalue` is a refcount race (a lost or doubled decref on shared objects can crash). So it crosses the bar set by CPython's iterator free-threading strategy ([gh-124397](https://github.com/python/cpython/issues/124397), Raymond Hettinger): even though "concurrent access is allowed to return duplicate/skipped values or raise", it must **not crash** — and this one corrupts state, which is why it has a dedicated fix PR. Sharing one `groupby` across threads is unusual (which caps real-world priority), but unlike `count`, `groupby` has **no** free-threading protection at all. Free-threaded build only.

## Suggested fix

Exactly what open **[PR #150792](https://github.com/python/cpython/pull/150792)** does: take the `groupby` object's per-object critical section (`Py_BEGIN_CRITICAL_SECTION`) over `groupby_next`'s read-modify-write of `currgrouper`/`currkey`/`currvalue`/`tgtkey` (and coordinate `_grouper_next`'s reads of the parent). The parent↔child grouper handoff (`currgrouper`) must be atomic with respect to concurrent advances.

## Notes

- **Already reported + fix in flight.** gh-150791 ("`groupby_next` data race on free-threaded builds") with open PR #150792 ("add critical section for `groupby.next`"). **No new filing warranted** — a confirmation on #150791 that `fusil --tsan` reproduces it is the only outward-facing step, at the maintainer's discretion. The isolated repro here (8 threads × `list(shared_groupby)`) is a simpler trigger than the issue's `__eq__`-key script. Re-run `repro.py` → `status: fixed` when #150792 merges.
- The merged gh-143543 / gh-146613 fixes are **re-entrancy** UAFs (the `Py_INCREF` snapshots in `groupby_next`), orthogonal to the thread race. The itertools FT-safety umbrella gh-123471 lists `pairwise`/`combinations`/`permutations`/`cwr`/`product`/… but not `groupby`; #150791/#150792 tracks `groupby` directly.
- Same shared-stateful-itertools-object class as `itertools.count` (TSAN-0006 / #153908). Distinct from the sequence-iterator cursor family (TSAN-0037/0038/0039/0040/0026).
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 10): the op-mix shared-object path shares one `groupby(range(...))` across workers each doing `list()`.

---

*This is gh-150791 (fix pending in PR #150792). Recorded for the catalog; not a separate filing.*
