# Data race: `decimal.Decimal.__hash__` caches its hash without synchronization (`_decimal.c:5924`)

*`dec_hash` lazily caches the computed hash in `self->hash` (`-1` sentinel) with a plain read/write. On a free-threaded build, two threads calling `hash()` on the same shared `Decimal` race on the cache field — one reads the `-1` sentinel while another stores the computed hash. `hash()` looks read-only to callers, so a shared `Decimal` is not safe to hash concurrently.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`Modules/_decimal/_decimal.c` memoizes a `Decimal`'s hash in the `hash` field of `PyDecObject`, initialized to `-1` and filled on first `__hash__`. The fill is an unsynchronized read-then-write:

```c
static Py_hash_t
dec_hash(PyObject *op)
{
    PyDecObject *self = _PyDecObject_CAST(op);
    if (self->hash == -1) {            /* :5924  read  */
        self->hash = _dec_hash(self);  /* :5925  write */
    }
    return self->hash;                 /* :5928 */
}
```

Two threads hashing the *same* `Decimal` for the first time concurrently produce a data race on `self->hash` (relaxed/plain access). It is value-benign (both compute the same hash and the store is a single aligned word), but it is a genuine TSan-reported data race on an operation callers treat as read-only.

## Reproducer

```python
import sys, threading
from decimal import Decimal
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 4
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker():
    for _ in range(ROUNDS):
        enter.wait()
        for d in pool[0]:
            hash(d)                     # dec_hash: read self->hash==-1, then write it
        leave.wait()

ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = [Decimal(f"{r}.{i}") for i in range(64)]   # fresh, unhashed each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= setarch -R env PYTHON_GIL=0 \
  TSAN_OPTIONS="halt_on_error=1 symbolize=1 history_size=4" \
  ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, glibc 2.43)

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x... by thread T3:
    #0 dec_hash    Modules/_decimal/_decimal.c:5924:15   (if (self->hash == -1))
    #1 PyObject_Hash Objects/object.c
  Previous write of size 8 at 0x... by thread T1:
    #0 dec_hash    Modules/_decimal/_decimal.c:5925:20   (self->hash = _dec_hash(self))
    #1 PyObject_Hash Objects/object.c
SUMMARY: ThreadSanitizer: data race Modules/_decimal/_decimal.c:5924:15 in dec_hash
```

Reproduces deterministically (exit 66) and does not crash — the racing value is identical on both threads.

## Root cause

`dec_hash` uses the standard lazy-hash-cache idiom (`hash == -1` sentinel, fill on demand) with no memory ordering. Under free-threading the read of the sentinel and the store of the result on two threads are a data race. The object is nominally immutable, so callers reasonably assume `hash(shared_decimal)` is thread-safe; the hidden mutable cache breaks that.

This mirrors the cached-hash treatment CPython already applies elsewhere (e.g. `unicode_hash` / other immutable types moving the cache field to atomic access for free-threading).

## Suggested fix

Use relaxed atomics on the cache field so the read/write are well-defined and TSan-clean (the value is idempotent, so relaxed ordering is sufficient):

```c
Py_hash_t h = _Py_atomic_load_ssize_relaxed(&self->hash);
if (h == -1) {
    h = _dec_hash(self);
    _Py_atomic_store_ssize_relaxed(&self->hash, h);
}
return h;
```

(`FT_ATOMIC_*` wrappers exist for exactly this pattern in the free-threaded build.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`). Low severity (value-benign, crash-free), but a real data race on a read-only-looking API. The same lazy-cache pattern should be audited across `_decimal` (any other memoized field) and other C types that cache a hash/repr in a plain field.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
