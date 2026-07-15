# Is a data race on a shared builtin's size (concurrent unsynchronized access) interesting?

**Short version:** on a free-threaded build, two threads that access the *same* `list` with no
user-level lock — one reading it (e.g. tuple-unpack, which reads `Py_SIZE`), the other resizing
it (`append`/`pop`, which stores `Py_SIZE`) — produce a ThreadSanitizer data race on the list's
size word: an **atomic relaxed store** vs a **plain (non-atomic) read**. We'd like to know whether
this class is considered actionable or expected "don't do that" behaviour, so we can triage it
correctly.

## Why we're asking

A ThreadSanitizer-based fuzzer (fusil `--tsan`) that hammers shared objects from many threads
surfaces ~half a dozen of these per run — always the same shape: a builtin container is read on one
thread while mutated on another, with no synchronization. They dedupe to a handful of signatures
(`_Py_SIZE_impl` vs `_Py_atomic_store_ssize_relaxed`, `list_resize` vs `binarysort`, etc.). We
suspect they're **not** interesting (unsynchronized access to a shared mutable object is a program
error, and the free-threading guarantee is crash-safety, not lock-free-correctness), but we'd
rather confirm than guess — and suppress the whole class if so.

## Minimal reproducer (stdlib only, ~25 lines)

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0 on a --disable-gil build"

shared = [0, 1, 2]
N = 200_000
start = threading.Barrier(2)

def unpacker():
    start.wait()
    for _ in range(N):
        try:
            a, b, c = shared          # UNPACK_SEQUENCE reads Py_SIZE(shared)
        except ValueError:
            pass                      # size changed under us; not what we're testing

def mutator():
    start.wait()
    for _ in range(N):
        shared.append(0)              # list_resize stores Py_SIZE(shared)
        shared.pop()

t1 = threading.Thread(target=unpacker)
t2 = threading.Thread(target=mutator)
t1.start(); t2.start(); t1.join(); t2.join()
print("done, no crash (TSan reports the race above)")
```

## How to run

Built CPython `--disable-gil --with-thread-sanitizer`. TSan needs ASLR disabled and (on Ubuntu)
the debuginfod lookup turned off, or `llvm-symbolizer` hangs on the currently-unreachable server:

```sh
DEBUGINFOD_URLS= setarch -R env PYTHON_GIL=0 \
  TSAN_OPTIONS="halt_on_error=1 symbolize=1 history_size=4" \
  ./python shared_list_race.py
```

## Confirmed output (CPython 3.16.0a0, `--disable-gil --with-thread-sanitizer`)

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x... by thread T1:
    #0 _Py_SIZE_impl                    Include/object.h:243
    #1 _PyEval_UnpackIterableStackRef   Python/ceval.c:2405
    #2 _PyEval_EvalFrameDefault         Python/generated_cases.c.h  (UNPACK_SEQUENCE)
    ...
  Previous atomic write of size 8 at 0x... by thread T2:
    #0 _Py_atomic_store_ssize_relaxed   Include/cpython/pyatomic_gcc.h:513
    #1 _Py_SET_SIZE_impl                Include/object.h:258
    ... (from list_resize on append/pop)
SUMMARY: ThreadSanitizer: data race Include/object.h:243 in _Py_SIZE_impl
```

## The specific asymmetry

`list_resize` publishes the new size with `_Py_atomic_store_ssize_relaxed` (an atomic store), but
`Py_SIZE` (here in `UNPACK_SEQUENCE`'s fast path) reads it with a **plain non-atomic load**. TSan
flags atomic-store-vs-plain-read as a race. Per the C11 model it is one; the question is whether
CPython intends `Py_SIZE` reads to be safe against a *concurrently-resizing* list at all (we assume
not — the caller is expected not to share a list across threads without its own lock).

The same shape appears for other builtin operations that read a container's size/state while
another thread mutates it (concurrent `list.sort`, slicing, etc.), so a single ruling covers the
whole class.

## Confirmed instances from fleet 01

Two fleet-01 signatures were traced to this same class (both on a shared `list`):

- **`bytes_join`** — `Include/cpython/pyatomic_gcc.h:_Py_atomic_store_ptr_release | Objects/stringlib/join.h:stringlib_bytes_join`.
  **Reproduced in isolation** (`notes/bytes_join_race.py`): one thread `append`s to a shared list
  (`list_resize` publishes the new `ob_item` with an atomic release store) while another `b"".join`s
  it (`stringlib_bytes_join` reads the items with a plain, non-atomic `PyList_GET_ITEM`). Same
  atomic-store-vs-plain-read asymmetry as the `_Py_SIZE` case. Read-while-mutate → **suppressed**.

- **`binarysort`** — `Objects/listobject.c:binarysort | Objects/listobject.c:binarysort`.
  This is **concurrent `list.sort()` of a shared list**, and it is subtler than the read-while-mutate
  cases: `list_sort_impl` takes **no critical section** on the list. It *detaches* the items array
  (`saved_ob_item = self->ob_item; Py_SET_SIZE(self, 0); self->ob_item = NULL`) and, with no
  `key=` func, sets `lo.keys = saved_ob_item` — so `binarysort` rewrites the list's **own array in
  place**. Two sorters that both read `ob_item` in the ~3-instruction window before the other
  detaches then rewrite the same array concurrently (`a[L] = pivot`). The observed instance stayed
  crash-safe (the slots hold valid objects, just reordered), but this is *mutate-while-mutate*
  without a lock, not merely read-while-mutate. **Not reproduced in isolation** — the detach window
  is microscopic (a plain multi-thread sort loop over 100k+ iterations never hit it), though the
  fleet did once. **Deliberately not suppressed**: held as a dev question for the umbrella issue
  (below) rather than folded into the "expected, ignore" bucket.

**Questions for CPython devs:**
1. Is the read-while-mutate class (concurrent unsynchronized *access* to a shared builtin — the
   `_Py_SIZE` and `bytes_join` cases) considered actionable, or expected/acceptable? If the latter
   we keep suppressing it and only report races in *internal* shared state (type caches, module
   state, extension objects) that a correct single-object-per-thread program can still hit.
2. Separately: is concurrent `list.sort()` on a shared list (the `binarysort | binarysort` case)
   intended to stay crash-safe? It runs without a per-object critical section and rewrites the
   detached array in place, so two racing sorts touch the same slots — we'd like confirmation that
   the detach scheme is sufficient and this is "don't do that" rather than a latent safety gap.
