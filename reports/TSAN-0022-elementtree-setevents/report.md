# Data race: `_elementtree.XMLParser._setevents` iterates a caller-supplied list with the unlocked `PySequence_Fast_GET_ITEM` macro (`_elementtree.c:4209`)

*`_setevents` calls `PySequence_Fast(events_to_report)`, which returns a real `list` argument **unchanged** (no copy), then walks it with `PySequence_Fast_GET_ITEM` — a raw `ob_item[i]` read that takes no per-object lock. If another thread appends to that same shared list, `list_resize` atomically republishes `ob_item` and memcpys the buffer out from under the reader: a TSan data race between `_setevents_impl` and `list_resize`. The racing state is the user's plain Python list, not the parser's internals, so this is the "shared-list mutated concurrently" class rather than a distinct XMLParser-state bug.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Modules/_elementtree.c`'s `_elementtree_XMLParser__setevents_impl` turns its `events_to_report` argument into a fast sequence and iterates it:

```c
if (!(events_seq = PySequence_Fast(events_to_report,      /* :4201 */
                                   "events must be a sequence"))) {
    return NULL;
}
for (i = 0; i < PySequence_Fast_GET_SIZE(events_seq); ++i) {          /* :4208 */
    PyObject *event_name_obj = PySequence_Fast_GET_ITEM(events_seq, i); /* :4209 read ob_item[i] */
    ...
}
```

`PySequence_Fast` returns a `list`/`tuple` argument **as-is** (just `Py_NewRef`) — it does not copy. So when the caller passes a real `list`, `events_seq` *is* that list, and `PySequence_Fast_GET_ITEM` expands to a raw, unlocked `((PyListObject*)events_seq)->ob_item[i]`. A second thread doing `events_list.append(...)` runs `list_resize`, which allocates a new `ob_item` array, `memcpy`s the old contents in, and republishes the pointer with `_Py_atomic_store_ptr_release(&self->ob_item, ...)`. The unsynchronized read of `ob_item[i]` races with that atomic store / memcpy.

This is the internal path taken by the public `xml.etree.ElementTree.XMLPullParser(events=...)` and `iterparse(source, events=...)` (both call `parser._setevents(queue, events)`).

## Reproducer

```python
import sys, threading
from xml.etree.ElementTree import XMLParser
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT_READ = 4
NT_APPEND = 4
ROUNDS = 4000
REPEAT = 40
APPENDS = 400
cur = [None]
enter = threading.Barrier(NT_READ + NT_APPEND + 1)
leave = threading.Barrier(NT_READ + NT_APPEND + 1)

def reader():
    p = XMLParser()      # own parser -> only the shared list races, not parser state
    q = []
    for _ in range(ROUNDS):
        enter.wait()
        L = cur[0]
        for _ in range(REPEAT):
            try: p._setevents(q, L)     # reads L->ob_item[i] via PySequence_Fast_GET_ITEM
            except BaseException: pass
        leave.wait()

def appender():
    for _ in range(ROUNDS):
        enter.wait()
        L = cur[0]
        try:
            for _ in range(APPENDS): L.append("end")   # list.append -> list_resize memcpy
        except BaseException: pass
        leave.wait()

ts  = [threading.Thread(target=reader)   for _ in range(NT_READ)]
ts += [threading.Thread(target=appender) for _ in range(NT_APPEND)]
for t in ts: t.start()
for r in range(ROUNDS):
    cur[0] = ["end"] * 8                  # fresh, short list -> keeps resizing each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no race detected")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

Each reader uses its **own** `XMLParser`, so the only cross-thread mutable state is the shared list — this isolates the `list_resize` face (the parser's own state does not race here). Reproduces deterministically (exit 66) on every run.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`)

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x7fffb6aa5c78 by thread T3:
    #0 _elementtree_XMLParser__setevents_impl  Modules/_elementtree.c:4209:36   (PySequence_Fast_GET_ITEM(events_seq, i))
    #1 _elementtree_XMLParser__setevents        Modules/clinic/_elementtree.c.h:1329
    ...
    #29 thread_run                              Modules/_threadmodule.c:388

  Previous atomic write of size 8 at 0x7fffb6aa5c78 by thread T8:
    #0 _Py_atomic_store_ptr_release  Include/cpython/pyatomic_gcc.h:565:3
    #1 list_resize                   Objects/listobject.c:165:6   (_Py_atomic_store_ptr_release(&self->ob_item, ...))
    #2 _PyList_AppendTakeRefListResize Objects/listobject.c:530
    #3 _PyList_AppendTakeRef          Include/internal/pycore_list.h:53
    #4 _PyEval_EvalFrameDefault       generated_cases.c.h:3981   (_CALL_LIST_APPEND)
    ...
    #27 thread_run                    Modules/_threadmodule.c:388

SUMMARY: ThreadSanitizer: data race Modules/_elementtree.c:4209:36 in _elementtree_XMLParser__setevents_impl
```

The seeded vehicle captured the same two functions with the write landing on the `memcpy` (`listobject.c:160`); this repro caught the sibling face — the `ob_item` pointer republish (`listobject.c:165`) — both inside `list_resize` from `_CALL_LIST_APPEND`. The signature (`_setevents_impl` <-> `list_resize` / `_Py_atomic_store_ptr_release`) matches exactly.

## Root cause

The `PySequence_Fast` + `PySequence_Fast_GET_SIZE` / `PySequence_Fast_GET_ITEM` idiom is the standard "fast path" for iterating a sequence in C, and it is safe under the GIL. Under free-threading it is only safe if the sequence cannot be mutated concurrently, because:

- `PySequence_Fast` (`Objects/abstract.c`) returns a `list`/`tuple` argument with just an incref — no defensive copy. So `events_seq` aliases the caller's live list.
- `PySequence_Fast_GET_ITEM(o, i)` on a list is the raw macro `PyList_GET_ITEM` = `((PyListObject*)o)->ob_item[i]`, with **no** lock and **no** atomic load of `ob_item`.
- `list_resize` (`Objects/listobject.c`) in the free-threaded build allocates a new `_PyListArray`, `memcpy`s the old items (`:160`), and republishes with `_Py_atomic_store_ptr_release(&self->ob_item, ...)` (`:165`), then defers the old buffer's reclamation (`free_list_items(old_items, _PyObject_GC_IS_SHARED(self))` at `:169`, which routes through `_PyMem_FreeDelayed` / QSBR when the list is shared).

The reader's plain read of `ob_item` / `ob_item[i]` races with that atomic store and memcpy. `_setevents` never copies `events_to_report` nor takes a critical section on it, so it inherits the general PySequence_Fast-on-a-shared-list hazard.

Note the racing memory is the **user's plain `list`**, not any `_elementtree` field. In the reproducer each thread has its own parser, and the race still fires — proof that the parser's internal state is not what races on this face.

### The signature's other face (shared *parser*, not shared *list*)

The seeded signature also lists a `self | self` face. That is a *different* race: two threads calling `_setevents` on **one shared XMLParser**. It is genuinely a race on the parser's own state — `Py_XSETREF(target->events_append, ...)` (`:4187`) and the `Py_CLEAR(target->*_event_obj)` block (`:4190`-`:4195`) are plain, unlocked writes (and `Py_XSETREF` is a refcount read-modify-write, so it can double-decref). It reproduces on its own (confirmed, `SUMMARY ... _elementtree.c:4187:5 in _elementtree_XMLParser__setevents_impl`, exit 66). That face is an unsynchronized-`TreeBuilder`-state bug, but it belongs to the "don't share one parser across threads" class (an XMLParser wraps a single stateful expat parser) rather than being unique to `_setevents`.

## Impact / severity

**Low, and largely expected.** For the assigned (`list_resize`) face:

- It is the "concurrently-mutated shared list" class (TSAN-0013): the racing object is a plain Python `list` the caller mutates from another thread while a stdlib C function reads it. Passing a container into a stdlib call and simultaneously mutating it from another thread is a user-side synchronization error, not a documented-safe pattern.
- Crash-safe in practice on this build: for a cross-thread *shared* list, `list_resize` reclaims the old `ob_item` buffer via QSBR (`_PyMem_FreeDelayed`), so the stale pointer the reader may observe still points at live (if stale) memory rather than freed memory. The read of `ob_item[i]` can nonetheless observe a stale/torn pointer or an out-of-bounds slot in principle, so it is a real, not merely theoretical, data race.
- No crash was observed; TSan halts on the race first (exit 66).

The `self`/`self` face is somewhat more serious (unlocked `Py_XSETREF` refcount RMW => potential double-decref / use-after-free), but it requires sharing one XMLParser across threads, which is already outside the object's single-threaded contract.

## Suggested fix

For the assigned face, the correct fix is primarily **caller-side**: do not mutate the `events` list from another thread while constructing/feeding a parser. If `_setevents` is to be hardened defensively (matching CPython's broader free-threading work on `PySequence_Fast` loops), it should snapshot the argument instead of aliasing it:

```c
/* copy once, then iterate the private snapshot */
events_seq = PySequence_List(events_to_report);   /* independent list; no aliasing */
if (events_seq == NULL) return NULL;
```

(or hold `Py_BEGIN_CRITICAL_SECTION(events_seq)` around the loop). This is a low-priority, generic hardening shared by many C functions that iterate caller sequences with the fast macros — not something specific to `_elementtree`.

For the `self`/`self` face, the internal writes to `target->events_append` and `target->*_event_obj` would need a critical section on the parser/target — but the pragmatic answer is that one XMLParser is not safe to drive from multiple threads.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), vehicle `xml_etree_cElementTree-...-tsanNEW-2` (an `XMLPullParser(events=<shared list>)`-style construction while sibling threads `list.append` the same shared list). This maps to the **shared-list mutation class** (cf. TSAN-0013), surfaced through `_elementtree._setevents`, rather than a distinct unsynchronized-XMLParser-state bug. The related `self`/`self` face is a genuine internal-state race but falls under the "don't share the parser" class (cf. TSAN-0009 / pyexpat). Both are catalogued here for the umbrella; neither looks like a strong candidate for an individual upstream filing.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
