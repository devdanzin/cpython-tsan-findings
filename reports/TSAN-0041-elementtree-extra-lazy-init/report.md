# Data race: `_elementtree` lazily initializes a shared `Element`'s `extra` struct without a critical section (`create_extra`, `Modules/_elementtree.c`)

*`xml.etree.ElementTree.Element` stores its attributes and children in a lazily-allocated `ElementObjectExtra` struct at `self->extra`. The C accelerator creates it on first need with an **unsynchronized** `if (!self->extra) create_extra(self, ...)`, and `create_extra` (`_elementtree.c:274`) does `self->extra = PyMem_Malloc(...)` with no lock. When one `Element` is shared across threads and more than one first-touches it (reads `.attrib`, calls `len()`, appends a child, …), two threads both observe `self->extra == NULL`, both run `create_extra`, and both **write** `self->extra` — a write/write data race (plus a leak of one `ElementObjectExtra`), and the write also races concurrent readers (`element_length`) and `clear_extra`.*

**This is not a new find:** it is [gh-149816](https://github.com/python/cpython/issues/149816) ("22 free-threading race conditions"), and the open (unmerged) [PR #149918](https://github.com/python/cpython/pull/149918) ("gh-149816: Fix race conditions in `Modules/_elementtree.c`") is the comprehensive fix. `fusil --tsan` (fleet 10) reproduced it independently; this report records the confirmation and a minimal isolated reproducer.

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`ElementObject` keeps its attribute dict and child list in a separately-allocated `ElementObjectExtra` (`self->extra`), created lazily so that leaf elements pay nothing. Every entry point that needs it uses the pattern:

```c
if (!self->extra) {
    if (create_extra(self, NULL) < 0)
        return NULL;
}
```

with **no** per-object critical section, and `create_extra` itself allocates and stores the pointer unlocked:

```c
LOCAL(int)
create_extra(ElementObject* self, PyObject* attrib)
{
    self->extra = PyMem_Malloc(sizeof(ElementObjectExtra));   /* :274  WRITE self->extra, unlocked */
    if (!self->extra) { PyErr_NoMemory(); return -1; }
    self->extra->attrib = Py_XNewRef(attrib);
    self->extra->length = 0;
    self->extra->allocated = STATIC_CHILDREN;
    self->extra->children = self->extra->_children;
    return 0;
}
```

When a single `Element` is shared across threads and two of them first-touch it concurrently, both see `self->extra == NULL` and both call `create_extra` → two `PyMem_Malloc` results are written to `self->extra` (write/write race; one buffer is leaked). The store also races concurrent readers of the field: `element_length` (`__len__`, reads `self->extra->length`) and `clear_extra` (frees `self->extra`). TSan reports the pair as `create_extra` vs `element_length` / `element_attrib_getter` / `clear_extra`.

## Reproducer

```python
import threading
import xml.etree.ElementTree as ET

# A shared Element whose `extra` struct is still NULL: concurrent `.attrib` reads each
# hit `if (!self->extra) create_extra(...)` and race the unlocked `self->extra = malloc()`.
NTHREADS = 8
ITERS = 4000
barrier = threading.Barrier(NTHREADS)


def worker(elem):
    barrier.wait()
    for _ in range(ITERS):
        _ = elem.attrib  # element_attrib_getter -> create_extra (lazy, unlocked)
        _ = len(elem)  # element_length reads self->extra


for _ in range(200):
    shared = ET.Element("tag")  # fresh: extra == NULL until first attrib/child access
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

Deterministic, exit **66**. Reproduces on **both** `debug-ft-nojit-tsan` and `release-ft-nojit-tsan` (release + TSan), so it is not a debug-build artifact.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, fleet build `a1d580430c8`)

```
WARNING: ThreadSanitizer: data race
  Write of size 8 by thread T6:
    #0 create_extra            Modules/_elementtree.c:274:17   (self->extra = PyMem_Malloc(...))
    #1 element_attrib_getter   Modules/_elementtree.c:2083:13  (if (!self->extra) create_extra(self, NULL))
    #2 getset_get              Objects/descrobject.c:194
    ...                                                        (obj.attrib)

  Previous read of size 8 by thread T5:
    #0 create_extra            Modules/_elementtree.c:274      (the other thread's lazy init)
    ...
SUMMARY: ThreadSanitizer: data race Modules/_elementtree.c:274 in create_extra
```

Fleet-10 drove two faces — `create_extra | element_length` (2 vehicles) and `clear_extra | create_extra` (1 vehicle); the isolated repro drives `create_extra | element_attrib_getter`. All three are the same unsynchronized `self->extra` lifecycle. (Full report in `tsan_report.txt`.)

## Root cause

`self->extra` is a lazily-initialized, mutable pointer on a `PyObject` that has **no** per-object locking on any of its accessors in the current C accelerator:

- **Writers** (unlocked): `create_extra` (`:274`, allocates + stores `self->extra`), `clear_extra` (frees + NULLs it), and every `if (!self->extra) create_extra(...)` call site (`element_attrib_getter`, `element_length`, `element_get`/`subscript`, `SubElement`/`append`, `set`, …).
- **Readers** (unlocked): `element_length` (`self->extra->length`), `element_get_attrib` (`self->extra->attrib`), the traverse/child accessors.

Because the check-and-create (`if (!self->extra) create_extra(...)`) is not atomic across threads, two threads racing the first touch of a shared `Element` both allocate and store `self->extra`. It is a genuine write/write C11 data race, it leaks one `ElementObjectExtra`, and — worse than value-benign — a reader can observe a half-initialized or about-to-be-overwritten `extra` pointer.

## Impact / severity

**Moderate.** `Element` is a *realistically* shared object (a parsed tree handed to worker threads for concurrent read), so the sharing pattern is not exotic the way sharing a single iterator is. The race is a write/write on a heap pointer: at minimum it leaks an `ElementObjectExtra` per lost race; at worst a concurrent reader dereferences a torn/overwritten/half-initialized `self->extra`, which can corrupt attributes/children or crash. It only affects the free-threaded (`--disable-gil`) build.

## Suggested fix

Exactly what open **[PR #149918](https://github.com/python/cpython/pull/149918)** does: take the `Element`'s per-object critical section (`Py_BEGIN_CRITICAL_SECTION(self)`) around the lazy check-and-create and around every `self->extra` reader/writer — `create_extra`'s field init, `element_attrib_getter`'s `if (!self->extra) create_extra(...)`, `element_get_attrib`, `element_length`, `clear_extra`, and traverse. With every accessor holding the section, the check-then-create is serialized (only one thread allocates), and reads never observe a torn pointer.

## Notes

- **Already reported + fix in flight.** gh-149816 enumerates the unsynchronized `_elementtree` `extra`/`attrib`/`text` races; PR #149918 (state: **OPEN**, unmerged as of the fleet build) is the comprehensive critical-section fix and covers this exact race (its diff wraps `create_extra`, `element_attrib_getter`, `element_get_attrib`, `element_length`, `clear_extra`, and traverse in `Py_BEGIN_CRITICAL_SECTION(self)`). [gh-146022](https://github.com/python/cpython/issues/146022) ("Make `xml.etree.ElementTree.Element` usable on free-threaded builds") is the broader tracking issue; an earlier attempt (gh-145568 / PR #145569) was **closed unmerged**, so no fix has landed yet — which is why this still reproduces on `main@a1d580430c8`. **No new filing is warranted.** The only outward-facing step, at the maintainer's discretion, is a confirmation note on #149816/#149918 that `fusil --tsan` reproduces it and that the PR resolves it. When #149918 merges, re-run `repro.py` and move this entry to `status: fixed`.
- **Distinct** from the other `_elementtree` FT entries in this catalog: TSAN-0022 (`SetEvents`/`TreeBuilder events`) and TSAN-0031 (`TreeBuilder` shared state). And distinct from the builtin-iterator cursor family (TSAN-0037/0038/0039/0040/0026) — this is a lazy-init-without-lock object field, not an iterator cursor.
- Found by ThreadSanitizer fuzzing (`fusil --tsan`, fleet 10), which shares one `Element` across worker threads via the op-mix shared-object path.

---

*Independent reproduction of gh-149816 / PR #149918; recorded for the `cpython-tsan-findings` catalog. Not separately filed.*
