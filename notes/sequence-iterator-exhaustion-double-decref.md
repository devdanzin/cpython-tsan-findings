# Sequence-iterator exhaustion double-DECREF (a systemic free-threading class)

**Found via fusil `--tsan` un-masking fleet-15, 2026-07-19.**

Every generic CPython sequence iterator drops its owning reference to the underlying
sequence in the *same* non-atomic way on exhaustion:

```c
    it->it_seq = NULL;      /* (or di_dict / si_set) -- non-atomic clear */
    Py_DECREF(seq);         /* drop the iterator's ONE owning ref */
    return NULL;
```

and the caller reads that reference (`seq = it->it_seq`) **unguarded** at the top of
`*_iternext`. Under `--disable-gil`, two threads advancing the **same** iterator to
exhaustion both read the same non-NULL `seq` and both `Py_DECREF(seq)` → the sequence's
refcount is dropped twice → **double-free / use-after-free**. Safe under the GIL (only one
thread runs `*_iternext` at a time); the exhaustion paths predate free-threading.

## The `it->it_seq = NULL; Py_DECREF(seq)` sites (main@a1d580430c8)

| container | function | site | status |
|-----------|----------|------|--------|
| **dict** (keys/values/items) | `dictiter_iternext_threadsafe` | `dictobject.c:6158-6159` (`di_dict`) | **TSAN-0053**, CRASHES 8/8, FILED **cpython#154130** |
| **set / frozenset** | `setiter_iternext` | `setobject.c:1130-1131` (`si_set`) | **TSAN-0054**, CRASHES 8/8, fix stalled in **cpython#144357** (open) |
| **str** | `unicodeiter_next` | `unicodeobject.c:14963-14964` (`it_seq`) | fleet caught 1 crash (`<object is freed>`); rarer in isolation (strings often interned/immortal → the extra DECREF is a no-op) |
| **str (ascii)** | `unicode_ascii_iter_next` | `unicodeobject.c:14986` (`it_seq`) | same shape |
| **bytes** | `striter_next` | `bytesobject.c:3451` (`it_seq`) | same shape; value race = TSAN-0037 |
| **tuple** | `tupleiter_next` | `tupleobject.c:1147` (`it_seq`) | same shape (not yet tripped) |
| **list** | `listiter_next` / reversed | `listobject.c:4080, 4238` (`it_seq`) | same shape (not yet tripped) |
| **generic seq** (`iter(obj)` on `__getitem__`) | `iter_iternext` | `iterobject.c:79` (`it_seq`) | same shape; cursor race = TSAN-0044 |

## Two crash signatures, by the sequence's lifetime

- **Throwaway sequence** (`iter({...})`, `iter(set(...))` — refcount ≈ 1, the iterator is the only holder):
  the second DECREF underflows immediately → `_Py_NegativeRefcount` at the `Py_DECREF` site
  (dictobject.c:6159 / setobject.c:1131), or a straight SIGSEGV on release.
- **Long-lived shared sequence** (a module-level `frozendict` / `frozenset` constant, refcount > 1):
  the underflow is **silent** — the refcount just drops below the live-reference count — until the
  next `gc.collect()`'s debug validation catches it: `Python/gc_free_threading.c` `validate_gc_objects`
  ("refcount is too small", :1116) or `update_refs` ("refcount >= 0", :999), reporting a wild refcount
  (~2^60, deferred/underflowed), or a later access UAFs. **This long-lived face is the more dangerous
  one** — it corrupts memory in a real program that merely iterates a shared module constant from threads,
  with no obvious link to the iterator.

## Disposition

- **dict** → filed as a standalone crash issue (**cpython#154130**) because its data-race face
  (cpython#148873) was *closed* as a dup of the value-benign gh-120496.
- **set** → **corroborate the open cpython#144356 / its stalled fix cpython#144357** with the crash
  reproducer (the PR's `CRITICAL_SECTION2(self, so)` + dropping the exhaustion DECREF under FT fixes it).
- **str/bytes/tuple/list/seqiter** → same root shape; the fix is uniform (serialize on the iterator and
  drop the ref exactly once, or atomic-exchange the `it_seq`/`di_dict`/`si_set` clear). Worth noting to
  the maintainers that this is a *class*, not three unrelated bugs — one review pass over every
  `*_iternext` exhaustion path would close all of them.

The value-benign *cursor* races (`it_index`/`si_pos`/`di_pos` advanced non-atomically — TSAN-0037/0038/
0039/0040/0044, gh-120496/gh-124397 "acceptable, don't crash") are a **different** defect in the same
functions; this class is the memory-safety one that crosses the "must not crash" bar.
