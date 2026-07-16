# Data race: `OrderedDict` iteration reads the linked-list head unlocked while `clear()` frees it (`odictobject.c:1961` vs `:806`)

*`OrderedDict` keeps insertion order in an internal doubly-linked list whose head is `od->od_first`. `iter(od)`/`list(od)` enters `odictiter_new`, which reads `od_first` (and then dereferences the head node's key) **without taking the per-object lock**. `OrderedDict.clear()` is `@critical_section`, so it takes the lock, sets `od_first`/`od_last` to `NULL`, and frees every node. Because the reader never acquires the same lock, the two race on `od_first` — and the reader can dereference a node the clearing thread has already freed. This is a genuine use-after-free, not a benign cache race.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Objects/odictobject.c` layers a doubly-linked list of `_ODictNode`s over the underlying dict to remember insertion order. The list head/tail live in the object:

```c
struct _odictobject {
    PyDictObject od_dict;
    _ODictNode *od_first;        /* first node in the linked list, if any */  /* :490 */
    _ODictNode *od_last;         /* last node in the linked list, if any */   /* :491 */
    ...
};
#define _odict_FIRST(od) (_PyODictObject_CAST(od)->od_first)   /* :528 */
#define _odict_LAST(od)  (_PyODictObject_CAST(od)->od_last)    /* :529 */
```

The mutators are correctly serialized under the object's per-object critical section, but the **iterator constructor is not**. `OrderedDict.clear()` acquires the lock (it is generated `@critical_section`) and, in `_odict_clear_nodes`, nulls the head/tail and frees every node:

```c
static void
_odict_clear_nodes(PyODictObject *od)
{
    PyMem_Free(od->od_fast_nodes);
    od->od_fast_nodes = NULL;
    od->od_fast_nodes_size = 0;
    od->od_resize_sentinel = NULL;

    node = _odict_FIRST(od);
    _odict_FIRST(od) = NULL;          /* :805  write od_first = NULL */
    _odict_LAST(od)  = NULL;          /* :806  write od_last  = NULL (TSan reports here) */
    while (node != NULL) {
        next = _odictnode_NEXT(node);
        _odictnode_DEALLOC(node);     /* :809  Py_DECREF(node->key); PyMem_Free(node) */
        node = next;
    }
    od->od_state++;
}
```

Meanwhile `iter(od)` / `list(od)` / `od.keys()` reaches `odictiter_new`, which reads the head pointer and immediately takes a new reference to the head node's key — all **without holding `od`'s lock**:

```c
static PyObject *
odict_iter(PyObject *op)
{
    return odictiter_new(_PyODictObject_CAST(op), _odict_ITER_KEYS);   /* :1552  no critical section */
}

static PyObject *
odictiter_new(PyODictObject *od, int kind)
{
    ...
    node = reversed ? _odict_LAST(od) : _odict_FIRST(od);   /* :1961  read od_first/od_last (racing) */
    di->di_current = node ? Py_NewRef(_odictnode_KEY(node)) : NULL;   /* :1962  deref node->key -> possible UAF */
    di->di_size = PyODict_SIZE(od);
    di->di_state = od->od_state;
    ...
}
```

Two threads — one clearing, one iterating — race on `od->od_first` (plain 8-byte read vs plain 8-byte write). TSan reports the write via `__tsan_memset` because the compiler lowers the adjacent `od_first = NULL; od_last = NULL;` stores to a memset; the racing word is the head/tail pointer field inside the `PyODictObject`.

## Reproducer

```python
import sys, threading
from collections import OrderedDict
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 8
ROUNDS = 3000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        od = pool[0]
        if wid % 2 == 0:
            od.clear()                    # _odict_clear_nodes: write od_first/od_last=NULL, free nodes
            for i in range(64):
                od[i] = i                 # repopulate so the LL head is non-NULL again
        else:
            try:
                list(od)                  # odict_iter -> odictiter_new: read od_first (unlocked)
            except RuntimeError:
                pass                       # "OrderedDict mutated during iteration" is fine
        leave.wait()

ts = [threading.Thread(target=worker, args=(w,)) for w in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = OrderedDict((i, i) for i in range(64))   # fresh, populated each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `heads/main:bcf98ddbc40`, `--disable-gil --with-thread-sanitizer`, Clang 21)

```
WARNING: ThreadSanitizer: data race (pid=2147915)
  Read of size 8 at 0x7fffb633b950 by thread T2:
    #0 odictiter_new      Objects/odictobject.c:1961          (node = _odict_FIRST(od))
    #1 odict_iter         Objects/odictobject.c:1552
    #2 PyObject_GetIter   Objects/abstract.c:2825
    #3 list_extend_iter_lock_held Objects/listobject.c:1282
    ...                   (list(od) on thread T2)

  Previous write of size 8 at 0x7fffb633b950 by thread T1:
    #0 __tsan_memset
    #1 _odict_clear_nodes    Objects/odictobject.c:806        (od_first/od_last = NULL)
    #2 OrderedDict_clear_impl Objects/odictobject.c:1226
    #3 OrderedDict_clear     Objects/clinic/odictobject.c.h:354
    ...                      (od.clear() on thread T1)

SUMMARY: ThreadSanitizer: data race Objects/odictobject.c:1961 in odictiter_new
```

Reproduces deterministically (exit 66) on every run. Signature matches the fleet-seeded race exactly: `odictiter_new` (read) vs `_odict_clear_nodes` (write) on `od_first`/`od_last`.

## Root cause

The `OrderedDict` locking discipline is **asymmetric**. Every mutator that touches the linked list runs under `od`'s per-object critical section:

- `OrderedDict.clear` is generated `@critical_section` (odictobject.c:1215) → `_odict_clear_nodes`;
- `_odict_clear_node`, `_odict_remove_node`, `_odict_resize`, `_odict_get_index_raw` all `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(od)`.

But the *iterator constructor* path does not lock:

- `odict_iter` (tp_iter, :1550), `odictkeys_iter` (:1973), and the items/values view iterators all call `odictiter_new` with **no** `Py_BEGIN_CRITICAL_SECTION(od)`.
- `odictiter_new` reads `_odict_FIRST(od)` / `_odict_LAST(od)` (:1961) as a plain load and then does `Py_NewRef(_odictnode_KEY(node))` (:1962), dereferencing the head node.

So a reader can (a) read a stale non-NULL `od_first` while a clearing thread nulls it — the raw data race TSan reports — and worse, (b) read a head node pointer, then have `_odict_clear_nodes` free that node (`_odictnode_DEALLOC` = `Py_DECREF(node->key); PyMem_Free(node)`, :719-723) before the reader executes `_odictnode_KEY(node)` / `Py_NewRef`, dereferencing freed heap and incref-ing a dangling `PyObject*`. That is a use-after-free, not merely a value-benign race like a cached hash.

The underlying dict is itself free-threading-safe, so callers reasonably expect `list(od)` / `iter(od)` on a shared `OrderedDict` to be as safe as on a plain `dict`. The missing lock on the odict-specific linked-list read breaks that expectation.

## Impact / severity

Medium-to-high. Unlike a value-benign cached-field race, the reader immediately dereferences and refs the node the writer may be freeing, so this can escalate from a reported data race to a real use-after-free → crash, refcount corruption, or torn read on a debug/ASan build or under memory pressure. It requires a shared `OrderedDict` iterated on one thread while cleared (or otherwise structurally mutated) on another — a normal method-level race (in scope; not the out-of-scope concurrent-`__init__` class). Trigger surface is broad: any `iter()`, `list()`, `reversed()`, `.keys()/.values()/.items()` iteration concurrent with `clear()`/`popitem()`/`move_to_end()`/deletion.

## Suggested fix

Take `od`'s critical section around the head/tail read and the head-key ref in the iterator constructor, matching the mutators. Either annotate the iterator entry points `@critical_section`, or wrap the body of `odictiter_new` (or its callers `odict_iter` / `odictkeys_iter` / items / values) in `Py_BEGIN_CRITICAL_SECTION(od) ... Py_END_CRITICAL_SECTION()` so that reading `od_first`/`od_last` and `Py_NewRef(_odictnode_KEY(node))` cannot interleave with `_odict_clear_nodes` freeing the nodes:

```c
static PyObject *
odictiter_new(PyODictObject *od, int kind)
{
    ...
    Py_BEGIN_CRITICAL_SECTION(od);
    node = reversed ? _odict_LAST(od) : _odict_FIRST(od);
    di->di_current = node ? Py_NewRef(_odictnode_KEY(node)) : NULL;
    di->di_size = PyODict_SIZE(od);
    di->di_state = od->od_state;
    Py_END_CRITICAL_SECTION();
    ...
}
```

(`odictiter_iternext` should be audited the same way — it also walks `di_current`/the node list and reads `od_state`; the whole iterator lifecycle needs to be consistent with the mutators' lock.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet `fusil-tsan_fleet_02`. Root cause is a missing per-object critical section on the `OrderedDict` iterator-construction path, while the mutation path (`clear`, etc.) is correctly locked — a locking-discipline gap specific to the odict linked-list overlay, distinct from the underlying dict's own (safe) synchronization. The same audit should cover the reverse/keys/items/values iterators and `odictiter_iternext`.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
