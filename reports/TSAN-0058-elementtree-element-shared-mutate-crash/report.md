# Crash: concurrent mutate/read of a shared `_elementtree.Element` corrupts child refcounts (`element_dealloc` abort) — the module has no critical sections

*`Modules/_elementtree.c` declares `Py_MOD_GIL_NOT_USED` but takes **no critical section** on any `Element` operation. A shared `Element`'s `extra` struct (children array + text/tail) is created/grown/cleared/freed (`create_extra` / `element_add_subelement` / `element_resize` / `clear_extra` / `dealloc_extra` / `_set_joined_ptr`) and read (`element_length` / `element_get_tail` / iteration) with plain, unsynchronized accesses. Concurrent `Element.clear()` / `SubElement()` racing `len(el)` / `el.tail` / `iter(el)` on one shared `Element` corrupt the child `PyObject*` refcounts, freeing a child (or the Element) while still referenced → `element_dealloc` (`_elementtree.c:704`) aborts on `assert(Py_REFCNT(op) == 0)`. This is the `Element`-object companion of TSAN-0031 (the `TreeBuilder` parse-state race) — same root (module-wide lack of locking), but the Element side **crashes**.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer and captured the backtrace; the maintainer reviewed and edited it._

## Reproducer

`repro.py` — one shared `Element` with 8 children; 8 threads, half mutating (`clear()` + `SubElement`), half reading (`len()` + child `.tail`):

```python
import threading
import xml.etree.ElementTree as ET
def mk():
    e = ET.Element("root")
    for i in range(8):
        c = ET.SubElement(e, "c%d" % i); c.text = "t"; c.tail = "x"
    return e
el = mk()
NT = 8; ITERS = 40000
def worker(w):
    for i in range(ITERS):
        try:
            if w % 2 == 0:
                el.clear()
                for j in range(4): ET.SubElement(el, "s%d" % j).tail = "y"
            else:
                _ = len(el)
                for ch in list(el): _ = ch.tail
        except Exception: pass
ts = [threading.Thread(target=worker, args=(w,)) for w in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
```

## Reproduction

- **Crash** (`debug-ft-nojit` and `debug-ft-nojit-asan`, `PYTHON_GIL=0`): **5/5** —
  `Modules/_elementtree.c:704: element_dealloc: Assertion 'Py_REFCNT(op) == 0' failed` (a child
  Element freed while still referenced / double-DECREF from the racing `clear_extra` ↔ reader). See
  `crash_backtrace.txt`.
- **TSan** (`debug-ft-nojit-tsan`): `WARNING: ThreadSanitizer: data race` across the `extra`/children
  machinery — `clear_extra | element_length`, `_set_joined_ptr | element_get_tail`,
  `create_extra | {dealloc_extra, element_length}`, `dealloc_extra | element_add_subelement`,
  `element_add_subelement | element_length`, etc. (`element_length` at `_elementtree.c:1628`).

## Scope

Concurrent **mutation** of a shared `Element` (the "shared mutable object with no locking" class, cf.
`multidict`, the shared-`Pickler` TSAN-0057). The module opts out of the GIL (`Py_MOD_GIL_NOT_USED`)
yet has zero critical sections, so even a single writer racing a reader of a shared parsed tree — a
plausible pattern — corrupts memory. `_elementtree` is on the gh-116738 audit list and named in
cpython#149816; the abandoned PR gh-145569 only touched `TreeBuilder.handle_end`. Distinct from
TSAN-0031, which is the `TreeBuilder` feed data race (no crash). How far to take `Element` FT-safety
is a maintainer call, but the manifestation here is memory-unsafety (premature free), not just
undefined tree contents.

## Suggested fix

Take the `Element`'s per-object critical section on its mutators and readers
(`clear`/`append`/`SubElement`/`resize`/tail-set and `len`/tail-get/iterate), part of the broader
`_elementtree` free-threading hardening the module still needs.
