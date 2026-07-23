# Crash: a shared `contextvars.Context` (HAMT) iterator corrupts its cursor and dereferences a wild node under free-threading (`hamt_iterator_next` / `hamt_iterator_bitmap_next`, `Python/hamt.c`)

*The HAMT iterator keeps its whole traversal cursor — `i_nodes[]`, `i_pos[]`, `i_level` — inside the iterator object and mutates it on every `next()` with **no synchronization**, and it stores the node pointers as **borrowed** references. Two threads advancing the same `Context` iterator desync `i_level` against `i_nodes[]`, so `hamt_iterator_next` reads `current = iter->i_nodes[iter->i_level]` as a stale/NULL/wild pointer and immediately does `IS_BITMAP_NODE(current)` → `Py_TYPE(current)` → **SEGV**. This is the `contextvars.Context` / HAMT sibling of the dict-iterator (TSAN-0053 / cpython#154130), set-iterator (TSAN-0054 / cpython#144357) and memoryview-iterator (TSAN-0055) shared-iterator crashes, and it crosses the gh-124397 "C iterators must not crash" bar.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer and captured the backtrace; the maintainer reviewed and edited it._

## Summary

`Python/hamt.c` (main@`a1d580430c8`). The iterator state lives in the iterator object and is advanced with plain reads/writes:

```c
static void
hamt_iterator_init(PyHamtIteratorState *iter, PyHamtNode *root) {
    for (uint32_t i = 0; i < _Py_HAMT_MAX_TREE_DEPTH; i++) {
        iter->i_nodes[i] = NULL;
        iter->i_pos[i] = 0;
    }
    iter->i_level = 0;
    /* Note: we don't incref/decref nodes in i_nodes. */   /* <- borrowed node pointers */
    iter->i_nodes[0] = root;
}

static hamt_iter_t
hamt_iterator_next(PyHamtIteratorState *iter, PyObject **key, PyObject **val) {
    if (iter->i_level < 0) {                     /* :2188  plain read of the cursor level */
        return I_END;
    }
    assert(iter->i_level < _Py_HAMT_MAX_TREE_DEPTH);
    PyHamtNode *current = iter->i_nodes[iter->i_level];   /* :2194  read node at current level */
    if (IS_BITMAP_NODE(current)) {               /* :2196  Py_TYPE(current) -> SEGV on a wild node */
        return hamt_iterator_bitmap_next(iter, key, val);
    }
    ...
}

static hamt_iter_t
hamt_iterator_bitmap_next(PyHamtIteratorState *iter, PyObject **key, PyObject **val) {
    int8_t level = iter->i_level;
    PyHamtNode_Bitmap *node = (PyHamtNode_Bitmap *)(iter->i_nodes[level]);
    Py_ssize_t pos = iter->i_pos[level];
    if (pos + 1 >= Py_SIZE(node)) {              /* :2092  Py_SIZE(node) -> SEGV on a wild node */
        iter->i_level--;                         /* :2097  plain write of the cursor level */
        return hamt_iterator_next(iter, key, val);
    }
    ...
}
```

There is **no critical section** anywhere in the HAMT iterator advance. `hamt_baseiter_tp_iternext` (`:2479`) calls straight into `hamt_iterator_next`, which reads and writes `i_level` / `i_pos[]` / `i_nodes[]` in place. Two threads calling `next()` on the same iterator interleave a reader of `i_level` (`:2188`, `:2194`) with a writer of `i_level` (`:2097`, and the deeper `i_pos[level]++` / `i_nodes[level+1] = ...` steps), so a thread can:

- read a decremented `i_level` and index `i_nodes[i_level]` that another thread has just `NULL`-ed out (line 2095, under `Py_DEBUG`) or not yet populated, or
- read `i_nodes[level]` at a level whose slot holds a stale node from an earlier descent,

then treat that stale/NULL/wild pointer as a node and evaluate `IS_BITMAP_NODE(current)` = `Py_TYPE(current) == &_PyHamt_BitmapNode_Type`, or `Py_SIZE(node)` — dereferencing a garbage pointer → **SIGSEGV**. Because the nodes are held **borrowed** (`i_nodes` are not INCREF'd), a concurrent structural change is not even required — cursor desync alone produces the wild deref.

Safe under the GIL (only one thread runs the advance at a time). The whole HAMT iterator predates free-threading and was not brought under the gh-124397 iterator hardening.

## What uses this

`Python/hamt.c`'s iterator backs `contextvars.Context` iteration: `iter(ctx)`, `ctx.keys()`, `ctx.values()`, `ctx.items()` all build a `hamt_baseiter` over the context's `ctx_vars` HAMT. A `Context` shared across threads (they are copyable/shareable objects — `copy_context()`, and the same object is often handed to several `Context.run()` calls) and iterated concurrently hits this. The crash backtrace shows the advance under `context_run` (`Python/context.c:731`).

## The gh-124397 contract

gh-124397 ("Strategy for Iterators in Free Threading") point 3: *"Other iterators implemented in C will get only the minimal changes necessary to cause them to **not crash** in a free-threaded build … Concurrent access is allowed to return duplicate values, skip values, or raise an exception."* Duplicate/skipped values would be acceptable here; a **SEGV is not**. The HAMT iterator is missing that minimal hardening — the same gap already fixed/being fixed for the builtin iterators in this class:

- **TSAN-0053 / cpython#154130** — dict iterator (`dictiter_iternext_threadsafe`)
- **TSAN-0054 / cpython#144357** — set iterator (`setiter_iternext`)
- **TSAN-0055** — memoryview iterator (`memoryiter_next`)
- rangeiter / str / bytes / tuple iterators (gh-124397, #153928)

## Reproducer

`repro.py` — populate a `Context` with 16 `ContextVar`s, then 8 threads share **one** `iter(ctx)`, advance it, and refill on exhaustion:

```python
import contextvars, threading
NT = 8; ITERS = 40000
vs = [contextvars.ContextVar(f"v{i}") for i in range(16)]
ctx = contextvars.copy_context()
def populate():
    for i, v in enumerate(vs):
        v.set(i)
ctx.run(populate)
cell = [iter(ctx)]
def worker():
    for _ in range(ITERS):
        it = cell[0]
        try: next(it)
        except StopIteration: cell[0] = iter(ctx)
        except Exception: pass
ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
```

## Reproduction

- **TSan** (`debug-ft-nojit-tsan`, `PYTHON_GIL=0`, `TSAN_OPTIONS=halt_on_error=1:exitcode=66`, `setarch -R`): `WARNING: ThreadSanitizer: data race … Python/hamt.c:2188 in hamt_iterator_next` (exit 66), signature `Python/hamt.c:hamt_iterator_next | Python/hamt.c:hamt_iterator_next`.
- **Crash** (`debug-ft-nojit`, plain free-threaded debug, no sanitizer, `PYTHON_GIL=0`): **SIGSEGV 6/6**. On `debug-ft-nojit-asan`: `AddressSanitizer: SEGV in _Py_TYPE_impl` (`Include/object.h:234`) ← `hamt_iterator_bitmap_next` (`hamt.c:2092`) ← `hamt_iterator_next` (`hamt.c:2196`) ← `hamt_baseiter_tp_iternext` (`hamt.c:2479`) ← `context_run` (`context.c:731`). See `crash_backtrace.txt`. The crash is not Py_DEBUG-only — it reproduces on the plain (no-sanitizer, no-`Py_DEBUG`-assert-dependent) free-threaded build as a raw SIGSEGV.

## Suggested fix

Bring the HAMT iterator advance under a per-iterator critical section (or make the cursor accesses atomic and bounds-check the borrowed `i_nodes[i_level]` before dereferencing), matching the `dictiter`/`setiter` hardening — minimal change to satisfy gh-124397's "must not crash" (duplicate/skipped entries are acceptable). Because `i_nodes` holds borrowed pointers, the guard must cover the whole `hamt_iterator_next` → `*_bitmap_next` / `*_array_next` descent, not just a single field.
