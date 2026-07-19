# Draft upstream comments (fleet-10 findings) — awaiting maintainer review before posting

Three drafted comments confirming existing issues with independent `fusil --tsan` reproducers.
All reproduced on `main@a1d580430c8` (3.16.0a0) on **both** `debug-ft-nojit-tsan` and
`release-ft-nojit-tsan` (release + TSan), so none are debug-only. Outward-facing — post at the
maintainer's discretion.

---

## → gh-149816 (or on PR #149918) — `_elementtree` item (69)

Independent confirmation of item **(69) "Unsynchronized extra pointer dereference in len in `Modules/_elementtree.c`"**, found by ThreadSanitizer fuzzing (`fusil --tsan`). Worth noting the race is a bit broader than a read-deref: it's a **write/write** on the lazily-allocated `self->extra`, because the `if (!self->extra) create_extra(...)` guard in the `extra` accessors isn't atomic. Two threads first-touching a shared `Element` both take the `!self->extra` branch and both run `create_extra`, which does `self->extra = PyMem_Malloc(...)` (`_elementtree.c:274`) with no critical section — so one allocation is overwritten (leaked) and readers can observe a torn pointer.

Minimal deterministic reproducer (exit 66 under TSan; `create_extra:274` as the write, reached via `element_attrib_getter` / `element_length`):

```python
import threading
import xml.etree.ElementTree as ET

NTHREADS = 8
barrier = threading.Barrier(NTHREADS)

def worker(elem):
    barrier.wait()
    for _ in range(4000):
        _ = elem.attrib          # if (!self->extra) create_extra(...) -- unlocked lazy init
        _ = len(elem)            # element_length reads self->extra

for _ in range(200):
    shared = ET.Element("tag")   # extra == NULL until first attrib/child touch
    ts = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
```

Confirmed still present on current `main` (3.16.0a0), on both a debug and a release `--with-thread-sanitizer` build. PR #149918's approach — taking `Py_BEGIN_CRITICAL_SECTION(self)` around the `if (!self->extra) create_extra(...)` check-and-create and the other `extra` accessors — covers this (both the read-deref and the write/write faces). Faces the fuzzer also hit: `create_extra | element_length` and `clear_extra | create_extra`.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → gh-144356 (or on PR #144357) — set iterator, the shared-*iterator* face

`fusil --tsan` independently hit this, via a **different trigger** than the report's `__length_hint__`-vs-set-mutation script: sharing a single **set iterator** across threads (no set mutation at all). `setiter_iternext` reads `si->si_pos` inside `Py_BEGIN_CRITICAL_SECTION(so)` (`setobject.c:1117`) but writes it back **outside** the section (`si->si_pos = i+1`, `:1128`), and `si->len--` is likewise unsynchronized — so two threads advancing the *same* iterator race on its private cursor. `setiter_len` reading `si->len` races the same write. The section is keyed on the set `so`, so it doesn't serialize concurrent use of one iterator.

```python
import operator, threading

NTHREADS = 8
barrier = threading.Barrier(NTHREADS)

def worker(it):
    barrier.wait()
    for _ in range(20000):
        try:
            next(it)                    # setiter_iternext: si->si_pos / si->len advance
        except StopIteration:
            pass
        operator.length_hint(it, 0)     # setiter_len: reads si->len

for _ in range(300):
    shared = iter(set(range(4096)))     # ONE shared iterator, no set mutation
    ts = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
```

Exit 66 under TSan; `setiter_iternext:1117` (read `si->si_pos`) vs `:1128` (write), plus the `setiter_len | setiter_iternext` (`si->len`) face. This is exactly the "concurrent use of the same iterator" case PR #144357 addresses by switching to `Py_BEGIN_CRITICAL_SECTION2(self, so)` and moving the cursor writes inside the section — so it's a useful corroboration of that expanded scope. Confirmed on current `main` (3.16.0a0), debug + release TSan builds.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; draft and reproducer by Claude Code, minimized and reviewed by hand.)*

---

## → gh-150791 (or on PR #150792) — groupby, a simpler reproducer

Independent confirmation via `fusil --tsan`, with a reproducer that doesn't need a custom key type — just several threads consuming one shared `groupby`:

```python
import itertools, threading

NTHREADS = 8
barrier = threading.Barrier(NTHREADS)

def worker(gb):
    barrier.wait()
    try:
        list(gb)                              # groupby_next on the shared gb
    except (RuntimeError, StopIteration, ValueError, TypeError):
        pass

for _ in range(500):
    shared = itertools.groupby(range(8192))   # ONE shared groupby
    ts = [threading.Thread(target=worker, args=(shared,)) for _ in range(NTHREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
```

Exit 66 under TSAN; the script above races `_grouper_create` against `groupby_next` (creating the child grouper while another thread advances the parent), alongside the `groupby_next`-vs-`groupby_next` race on `currgrouper` described in the issue. Also iterating the yielded sub-groups races `_grouper_next` against the parent's `currkey`/`currvalue` — consistent with PR #150792 guarding `_grouper_next` (via `Py_BEGIN_CRITICAL_SECTION(parent)`) as well as `groupby_next`. Confirmed on current `main` (3.16.0a0), debug + release TSan builds.

*(Found by `fusil --tsan`, a ThreadSanitizer fuzzer; draft and reproducer by Claude Code, minimized and reviewed by hand.)*
