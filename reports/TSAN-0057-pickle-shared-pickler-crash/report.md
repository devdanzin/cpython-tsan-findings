# Crash: a shared `_pickle.Pickler` corrupts its memo table (and output buffer) under concurrent `dump()` / `clear_memo()` in free-threading (`PyMemoTable_Clear` vs `_PyMemoTable_Lookup` / `PyMemoTable_Size`, `Modules/_pickle.c`)

*A `Pickler` object takes **no per-object critical section**, so using one shared `Pickler` from several threads races its internal `PyMemoTable` and output buffer. `Pickler.clear_memo()` (`PyMemoTable_Clear`) walks `mt_table` doing `Py_XDECREF(me_key)`, then zeroes it (`memset`) and resets `mt_used`, while a concurrent `Pickler.dump()` reads the same table through `PyMemoTable_Get` → `_PyMemoTable_Lookup` (and reads `mt_used` via `PyMemoTable_Size`, and may resize `mt_table` in `memo_put`). The result is a **double-DECREF of a memo'd key (`_Py_NegativeRefcount`)**, plus output-buffer corruption in `_Pickler_Write` and a table use-after-free/read-of-freed on resize — a memory-unsafe crash, not just wrong output.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer and captured the backtraces; the maintainer reviewed and edited it._

## Summary

`Modules/_pickle.c` (main@`a1d580430c8`). The Pickler memo table is manipulated with plain, unsynchronized accesses:

```c
static Py_ssize_t
PyMemoTable_Size(PyMemoTable *self) {
    return self->mt_used;                        /* :807  plain read */
}

static int
PyMemoTable_Clear(PyMemoTable *self) {
    Py_ssize_t i = self->mt_allocated;
    while (--i >= 0) {
        Py_XDECREF(self->mt_table[i].me_key);    /* :816  DECREF every key */
    }
    self->mt_used = 0;                           /* :818  plain write */
    memset(self->mt_table, 0, self->mt_allocated * sizeof(PyMemoEntry));  /* :819 */
    return 0;
}

static PyMemoEntry *
_PyMemoTable_Lookup(PyMemoTable *self, PyObject *key) {
    ...
    PyMemoEntry *table = self->mt_table;         /* :842  plain read of the table pointer */
    ...
    entry = &table[i];
    if (entry->me_key == NULL || entry->me_key == key)   /* :848  read me_key */
        return entry;
    ...
}
```

`Pickler.dump()` → `dump()` → `save()` → `PyMemoTable_Get()` → `_PyMemoTable_Lookup()` reads `mt_table` / `mt_table[i].me_key`; `memo_put()` reads/writes `mt_used` and can call `_PyMemoTable_ResizeTable()` (which `PyMem_Realloc`s `mt_table`, freeing the old array). `Pickler.clear_memo()` → `PyMemoTable_Clear()` `Py_XDECREF`s every `me_key` and `memset`s the table. None of these take a critical section on the Pickler, so two threads sharing one `Pickler` race:

- **`PyMemoTable_Clear`'s `Py_XDECREF(me_key)`** vs another thread's memo operation on the same key → the key's refcount is dropped twice → **`_Py_NegativeRefcount`** (double-DECREF / premature free).
- **`PyMemoTable_Clear`'s `memset` / `_PyMemoTable_ResizeTable`'s realloc** vs **`_PyMemoTable_Lookup`'s read of `mt_table[i].me_key`** → read of zeroed/freed table memory.
- **`_Pickler_Write`** (`_pickle.c:1106`) — two `dump()`s racing the shared `self->output_buffer` / framer.

Safe under the GIL (one thread in the Pickler at a time).

## Reproducer

`repro.py` — one shared `Pickler`, 4 threads `dump()` + 4 threads `clear_memo()`:

```python
import _pickle, io, threading
p = _pickle.Pickler(io.BytesIO())
N = 30000
def dumper():
    for _ in range(N):
        try: p.dump([1, 2, 3, "x", {"k": 1}])
        except Exception: pass
def clearer():
    for _ in range(N):
        try: p.clear_memo()
        except Exception: pass
ts = [threading.Thread(target=dumper) for _ in range(4)] + \
     [threading.Thread(target=clearer) for _ in range(4)]
for t in ts: t.start()
for t in ts: t.join()
print("done-pickle")
```

## Reproduction

- **TSan** (`debug-ft-nojit-tsan`, `PYTHON_GIL=0`, `setarch -R`): `WARNING: ThreadSanitizer: data race` in `PyMemoTable_Size` (`:807`) / `_PyMemoTable_Lookup` (`:848`) vs `PyMemoTable_Clear` (`:819`) — the fleet's `tsan_races.tsv` recorded `PyMemoTable_Clear | _PyMemoTable_Lookup` and `PyMemoTable_Clear | PyMemoTable_Size`. Also `_pickle_Pickler_dump_impl | _pickle_Pickler_dump_impl` (dump-vs-dump on the output buffer).
- **Crash** (`debug-ft-nojit`, plain free-threaded debug, no sanitizer, `PYTHON_GIL=0`): **crashes 6/6** — `Include/refcount.h:520: _Py_NegativeRefcount: object has negative ref count` (SIGABRT), and a SIGSEGV face in `_Pickler_Write` (`_pickle.c:1106`). See `crash_backtrace.txt`. Not Py_DEBUG-only (also SIGSEGV on the plain build).

## Classification / scope

This is the "shared mutable object with no per-object locking" class (cf. the `multidict` C hashtable, the shared-list races TSAN-0013/0014). It is **distinct from the known Unpickler-memo bug cpython#150505 / PR #150550**, which is the *Unpickler* memo (`_Unpickler_MemoPut`, a separate array); this is the *Pickler* `PyMemoTable`. Whether concurrent use of one `Pickler` across threads is a scenario CPython wants to make crash-safe is a maintainer call — but the manifestation here is memory-unsafety (double-DECREF / UAF), not merely undefined pickle output. `_pickle` is on the gh-116738 "audit all built-in modules for thread safety" list and is one of the modules named in cpython#149816.

## Suggested fix

If concurrent Pickler use is to be crash-safe, take a per-`Pickler` critical section around `dump()` / `clear_memo()` (and the memo-table + output-buffer mutators), matching the approach used for the free-threading hardening of other stateful C objects. Otherwise, document that a `Pickler`/`Unpickler` instance must not be shared across threads without external locking.
