# Crash: a shared `memoryview` iterator double-DECREFs `it_seq` in its exhaustion path under free-threading (`memoryiter_next`, `Objects/memoryobject.c:3641-3642`)

*`memoryiter_next` reads `seq = it->it_seq` unguarded and, on exhaustion, runs `it->it_seq = NULL; Py_DECREF(seq);` with **no critical section at all**. Two threads advancing the same memoryview iterator to exhaustion both read the same non-NULL `seq` and both `Py_DECREF(seq)` → the memoryview's refcount underflows → **use-after-free**. This is the memoryview-iterator sibling of the dict-iterator (TSAN-0053 / cpython#154130) and set-iterator (TSAN-0054 / cpython#144357) exhaustion double-DECREFs — the same systemic `it_seq = NULL; Py_DECREF(seq)` shape, listed as "same shape (not yet tripped)" in that class's writeup and now tripped.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducers and captured the backtraces; the maintainer reviewed and edited it._

## Summary

`Objects/memoryobject.c` (main@`a1d580430c8`):

```c
static PyObject *
memoryiter_next(PyObject *self)
{
    memoryiterobject *it = (memoryiterobject *)self;
    PyMemoryViewObject *seq;
    seq = it->it_seq;                       /* :3623  plain read of the owning ref */
    if (seq == NULL) {
        return NULL;
    }

    if (it->it_index < it->it_length) {     /* :3628  cursor read */
        CHECK_RELEASED(seq);
        Py_buffer *view = &(seq->view);
        char *ptr = (char *)seq->view.buf;

        ptr += view->strides[0] * it->it_index++;   /* :3633  cursor write */
        ptr = ADJUST_PTR(ptr, view->suboffsets, 0);
        if (ptr == NULL) {
            return NULL;
        }
        return unpack_single(seq, ptr, it->it_fmt);
    }

    it->it_seq = NULL;                      /* :3641  non-atomic clear */
    Py_DECREF(seq);                         /* :3642  drop the iterator's one owning ref */
    return NULL;
}
```

Unlike `dictiter`/`setiter`, `memoryiter_next` takes **no critical section whatsoever** — neither around the cursor advance nor around the exhaustion clear-and-DECREF. The iterator holds exactly **one** reference to the memoryview (`it_seq`). Two threads `T1`/`T2` calling `next()` on the same near-exhausted iterator interleave as:

- `T1`: `seq = it->it_seq` (non-NULL)
- `T2`: `seq = it->it_seq` (same non-NULL)
- both find `it_index >= it_length` (exhausted) and both run `it->it_seq = NULL; Py_DECREF(seq)`

→ the memoryview's refcount is dropped twice → **double-free / use-after-free**. Safe under the GIL (only one thread runs `memoryiter_next` at a time); the exhaustion path predates free-threading.

## Two races, one signature

Both of the following race in `memoryiter_next` and collapse to the single fusil `tsan_dedup` signature `Objects/memoryobject.c:memoryiter_next | Objects/memoryobject.c:memoryiter_next`:

1. **Exhaustion double-DECREF (memory-safety / this crash):** read `seq = it->it_seq` (`:3623`) vs write `it->it_seq = NULL` (`:3641`) + `Py_DECREF(seq)` (`:3642`). Crosses the gh-124397 "must not crash" bar.
2. **Cursor race (value-benign):** read `it->it_index < it->it_length` (`:3628`) vs write `it->it_index++` (`:3633`). The memoryview member of the value-benign `it_index` cursor family (TSAN-0037 bytes, TSAN-0044 seqiter, gh-120496 / gh-124397 — "acceptable, must not crash"). This one only duplicates/skips an element.

The catalog entry is classified by its worst face (the crash).

## Reproducer

`repro.py` — 8 threads share one memoryview iterator (`iter(memoryview(bytearray(range(32))))`), refilled on exhaustion; every thread races the exhaustion clear-and-DECREF:

```python
import threading
NT = 8; ITERS = 200000
def newit(): return iter(memoryview(bytearray(range(32))))
cell = [newit()]
def worker():
    for _ in range(ITERS):
        it = cell[0]
        try: next(it)
        except StopIteration: cell[0] = newit()
        except Exception: pass
ts = [threading.Thread(target=worker) for _ in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
```

Run: `PYTHON_GIL=0 <ft-python> repro.py`.

- **debug-ft-nojit** (`--disable-gil --with-pydebug`, main@`a1d580430c8`): aborts **4/4** within seconds — `Objects/memoryobject.c:3642: _Py_NegativeRefcount: Assertion failed: object has negative ref count / <object ... is freed>` (or a straight SIGSEGV). See `crash_backtrace.txt`.
- **release-ft-nojit-asan** (`--disable-gil`, no debug): **SEGV in `memoryiter_next` (`memoryobject.c:3629`) → `builtin_next` → `_PyEval_EvalFrameDefault`**, dereferencing the freed memoryview — a genuine use-after-free on a non-debug build, **not** a debug-only tripwire.
- Plain `release-ft-nojit-o0` (no sanitizer) did not fault in a few runs — expected for a UAF where the freed block isn't immediately reused; the ASan build makes the memory-unsafety deterministic.

## Disposition

The value-benign cursor face is acceptable per the FT iterator strategy (gh-124397; gh-120496 closed). The **crash** face is not: it is the same `it_seq = NULL; Py_DECREF(seq)` exhaustion double-free as the **dict** iterator (TSAN-0053, filed standalone as **cpython#154130** because its data-race face #148873 was closed as a dup of the value-benign gh-120496) and the **set** iterator (TSAN-0054, whose open fix PR **cpython#144357** for #144356 has stalled since 2026-05). `memoryiter_next` is worse than both: it takes **no critical section at all**.

This is one more crashing face of a **class**, not an isolated bug — `str`/`bytes`/`tuple`/`list`/generic-seqiter share the identical shape (see `../../notes/sequence-iterator-exhaustion-double-decref.md`). The uniform fix is to serialize concurrent `next()` on the same iterator and drop the owning reference exactly once (e.g. move the `Py_DECREF(seq)` to `tp_dealloc` and make exhaustion sticky, as cpython#144357 does for sets), rather than clearing `it_seq` + `Py_DECREF` inside `tp_iternext`.

**Best move:** rather than a fresh filing, corroborate the class — add the memoryview crash reproducer as a data point on the open FT-iterator work (cpython#124397 for the strategy, or the umbrella cpython#153852), noting memoryview joins dict/set as a *crashing* (not merely value-benign) exhaustion double-DECREF and that `memoryiter_next` uniquely takes no critical section. (Maintainer's call; outward-facing, awaiting go-ahead.)
