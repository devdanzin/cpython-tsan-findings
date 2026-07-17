# Data race: concurrent feed of a shared `_elementtree.TreeBuilder` races its own unsynchronized parse state (`self->data` / `self->last` / `self->this` / `self->index` / `self->stack`) — `Modules/_elementtree.c`, no critical section

*A `TreeBuilder` keeps its entire parse state in plain `TreeBuilderObject` struct fields. Every public feed method (`start`/`data`/`end`/`comment`/`pi` → `treebuilder_handle_*`) mutates those fields in place — `Py_CLEAR`/`Py_SETREF`/`Py_NewRef` on the pointer fields, `index++`/`index--` on the stack cursor — with **no lock and no `@critical_section`** (`Modules/_elementtree.c` contains zero `Py_BEGIN_CRITICAL_SECTION`, on the build and on current `main`). Two threads calling any of these methods on the **same** shared builder therefore data-race on the builder's internals. This is a whole cluster of ~8 TSan signatures, all `treebuilder_*` vs `treebuilder_*`.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`_elementtree.TreeBuilder` is the incremental tree sink used by `XMLParser`, `iterparse`, and any code that drives it directly. Its state lives in the `TreeBuilderObject` struct (`Modules/_elementtree.c:2393`):

```c
typedef struct {
    PyObject_HEAD
    PyObject *root;          /* root node (first created node) */
    PyObject *this;          /* current node */
    PyObject *last;          /* most recently created node */
    PyObject *last_for_tail; /* most recently created node that takes a tail */
    PyObject *data;          /* data collector (string or list), or NULL */
    PyObject *stack;         /* element stack */
    Py_ssize_t index;        /* current stack size (0 means empty) */
    ...
} TreeBuilderObject;
```

Every public feed method mutates these fields **in place, with no synchronization**:

- `.start()` → `treebuilder_handle_start`: `treebuilder_flush_data(self)`, then reads `self->this`, appends to `self->stack`, `self->index++`, `Py_SETREF(self->this, …)`, `Py_SETREF(self->last, …)`, `Py_CLEAR(self->last_for_tail)`.
- `.data()` → `treebuilder_handle_data`: reads `self->last`, writes `self->data`.
- `.end()` → `treebuilder_handle_end`: `treebuilder_flush_data(self)`, reads `self->stack`, `self->index--`, writes `self->last`/`self->last_for_tail`/`self->this`.
- `.comment()` → `treebuilder_handle_comment` and `.pi()` → `treebuilder_handle_pi`: `treebuilder_flush_data(self)` (+ optional `self->last_for_tail` write).
- The shared helper `treebuilder_flush_data` reads `self->data` (`:2684`) and, via `treebuilder_extend_element_text_or_tail`, **writes** `self->data` (`Py_CLEAR`/`*data = NULL`, `:2636`/`:2644`).

None of the clinic methods (`_elementtree_TreeBuilder_start` / `_data` / `_end` / `_comment` / `_pi` / `_close`) takes `@critical_section` and the module defines **no** `Py_BEGIN_CRITICAL_SECTION` anywhere. So two threads feeding one shared builder race on `self->data`, `self->last`, `self->this`, `self->last_for_tail`, `self->index`, and `self->stack`. Because these are the same few struct words touched by *every* handler, the fuzzer produced ~8 distinct `treebuilder_* | treebuilder_*` signatures (~18 vehicles in fleet-03) — all faces of the same "unsynchronized shared-builder parse state" bug.

## Reproducer

`repro.py` shares **one** `ET.TreeBuilder()` across 6 worker threads that each call `start`/`data`/`comment`/`pi`/`end` in a barrier loop (fresh builder per round to keep the just-started fields hot):

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"
import xml.etree.ElementTree as ET

N = 6
ROUNDS = 4000
box = [None]
enter = threading.Barrier(N + 1)
leave = threading.Barrier(N + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        tb = box[0]
        for i in range(8):
            try:
                tb.start("t%d" % (i & 3), {})   # handle_start (flush_data + writes)
                tb.data("x")                    # handle_data  (reads self->last, writes self->data)
                tb.comment("c")                 # handle_comment (flush_data)
                tb.pi("p", "d")                 # handle_pi     (flush_data)
                tb.end("t%d" % (i & 3))         # handle_end    (flush_data + writes self->last/index)
            except Exception:
                pass
        leave.wait()

threads = [threading.Thread(target=worker, args=(w,)) for w in range(N)]
for t in threads: t.start()
for r in range(ROUNDS):
    box[0] = ET.TreeBuilder()   # fresh shared builder each round
    enter.wait(); leave.wait()
for t in threads: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
setarch -R env -u PYTHON_GIL PYTHON_GIL=0 \
  TSAN_OPTIONS='halt_on_error=1:symbolize=1:exitcode=66:history_size=4' \
  DEBUGINFOD_URLS= \
  bash -c 'ulimit -v unlimited; exec ./python repro.py'
```

**Exit 66 on 8/8 runs (deterministic, within ~1 s).** Under `halt_on_error=1` TSan stops at the first race; across runs the SUMMARY names different faces of the cluster — most often `treebuilder_flush_data` reading `self->data` racing `treebuilder_extend_element_text_or_tail` writing it, sometimes `treebuilder_handle_start` reading `self->this` (`:2772`) racing `treebuilder_handle_end` writing it (`:2872`). This repro uses **no XMLParser and no expat** — a bare `TreeBuilder` isolates the builder-internal race.

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `_elementtree.cpython-316td`)

```
WARNING: ThreadSanitizer: data race
  Read of size 8 at 0x7fffb67c81d0 by thread T2:
    #0 treebuilder_flush_data                 Modules/_elementtree.c:2684:16   (if (!self->data))
    #1 treebuilder_handle_start               Modules/_elementtree.c:2749:9
    #2 _elementtree_TreeBuilder_start_impl     Modules/_elementtree.c:3105:12
    ... TreeBuilder.start()

  Previous write of size 8 at 0x7fffb67c81d0 by thread T6:
    #0 treebuilder_extend_element_text_or_tail Modules/_elementtree.c:2636:19   (*data = NULL, i.e. self->data = NULL)
    #1 treebuilder_flush_data                  Modules/_elementtree.c
    #2 treebuilder_handle_comment              Modules/_elementtree.c:2890:9
    ... TreeBuilder.comment()

SUMMARY: ThreadSanitizer: data race Modules/_elementtree.c:2684:16 in treebuilder_flush_data
```

`tsan_report.txt` holds the full raw block. The seeding fleet vehicle (`xml_etree_ElementTree-…-tsanNEW`) showed the same field via `treebuilder_extend_element_text_or_tail:2644` (write) racing `treebuilder_flush_data:2684` (read) reached through `TreeBuilder.start()` / `TreeBuilder.pi()` — same `self->data`, same pair.

## Root cause

`self->data` is written NULL with no synchronization in the flush fast path (`Modules/_elementtree.c:2627`):

```c
static int
treebuilder_extend_element_text_or_tail(elementtreestate *st, PyObject *element,
                                        PyObject **data, PyObject **dest, PyObject *name)
{
    if (Element_CheckExact(st, element)) {
        PyObject *dest_obj = JOIN_OBJ(*dest);
        if (dest_obj == Py_None) {
            *dest = JOIN_SET(*data, PyList_CheckExact(*data));
            *data = NULL;                 // :2636  WRITE self->data  (data == &self->data)
            Py_DECREF(dest_obj);
            return 0;
        }
        else if (JOIN_GET(*dest)) {
            if (PyList_SetSlice(dest_obj, PY_SSIZE_T_MAX, PY_SSIZE_T_MAX, *data) < 0)
                return -1;
            Py_CLEAR(*data);              // :2644  WRITE self->data = NULL
            return 0;
        }
    }
    ...
}
```

and read plainly in the same helper (`:2682`):

```c
LOCAL(int)
treebuilder_flush_data(TreeBuilderObject* self)
{
    if (!self->data) {                    // :2684  READ self->data
        return 0;
    }
    ...
    return treebuilder_extend_element_text_or_tail(st, element, &self->data, ...);
}
```

`treebuilder_flush_data` is called at the top of `treebuilder_handle_start` (`:2749`), `_handle_end` (`:2854`), `_handle_comment` (`:2890`), and `_handle_pi` (`:2928`), so any two of those on one shared builder race on `self->data`. The same shape holds for the other fields — e.g. `treebuilder_handle_start` reads `this = self->this;` (`:2772`) while `treebuilder_handle_end` does `self->this = Py_NewRef(PyList_GET_ITEM(self->stack, self->index));` (`:2872`), and `treebuilder_handle_data` reads `if (self->last == Py_None)` (`:2817`) while `treebuilder_handle_end` does `self->last = Py_NewRef(this);` (`:2869`).

This is not a single missed atomic load: the entire `TreeBuilderObject` is treated as single-threaded, and the writes are not plain word stores but **refcount RMW** (`Py_CLEAR`/`Py_SETREF`/`Py_NewRef`/`Py_DECREF`) and a non-atomic `self->index++`/`self->index--` on the stack cursor. There is no per-object critical section to serialize them.

## Impact / severity

**Moderate (crash-capable in principle; no crash observed because TSan halts on the plain-memory race first).** The confirmed report is a benign-looking pointer read/write on `self->data`, but the surrounding unsynchronized operations are stronger than a value race:

- `Py_CLEAR`/`Py_SETREF`/`Py_DECREF` on `self->data`/`self->last`/`self->this`/`self->last_for_tail` are refcount read-modify-write on shared `PyObject*` fields — concurrent execution can drop a refcount twice or lose a decref → use-after-free / leak.
- `self->index++` / `self->index--` are non-atomic on a cursor that indexes `self->stack` via `PyList_GET_ITEM(self->stack, self->index)` (`:2872`); a lost update can drive `index` out of range → out-of-bounds list access.

In practice a `TreeBuilder` is conventionally single-threaded (the sink of one parser feeding one document), which lowers real-world priority. But `_elementtree` declares `Py_MOD_GIL_NOT_USED`, asserting free-threading safety it does not deliver here — a shared `TreeBuilder` hammered from two threads should not data-race on its own internal state.

## Real bug vs. expected

**Real CPython free-threading bug, in scope.** `_elementtree.c` opts into free-threading (`Py_MOD_GIL_NOT_USED`) but leaves the whole `TreeBuilder` feed path unlocked. This is a normal method race on a shared builtin (repeated `start`/`data`/`end`/… calls), not concurrent `__init__`/construction (cf. cpython#127192, out of scope), and it is neither an expat/libc race nor a subinterpreter race. Sharing an accelerator object across threads and calling its methods is exactly the pattern the FT audit (gh-116738) is meant to make safe.

## How this differs from existing catalog entries

- **TSAN-0009 (pyexpat, "don't share the parser").** That race is in the **expat XMLParser**'s parse state / the C expat allocator, reached by feeding a shared *parser*. This finding needs **no XMLParser and no expat**: it is the pure-Python-facing `TreeBuilder`'s own struct fields, hit by calling `TreeBuilder.start/data/end/comment/pi` directly. (One of the four fleet vehicles, `inst-01/_elementtree`, happened to print an `expat_malloc` race first under `halt_on_error` — that is the TSAN-0009 face firing first in a mixed vehicle, not this bug; the repro isolates the builder.)
- **TSAN-0013 / TSAN-0022 (`_elementtree` `_setevents`).** Those are about a caller-supplied **events list**'s `ob_item` buffer (`list_resize`) aliased through `XMLParser._setevents`, plus a self/self face on the parser's `events_append`/`*_event_obj` refcount fields. This finding is **not a list** and **not the events fields** — it is the builder's `data`/`last`/`this`/`last_for_tail`/`index`/`stack` parse-state, raced by the feed handlers themselves.

## Suggested fix

Apply the module's own `_lock_held` split-function + `Py_BEGIN_CRITICAL_SECTION(self)` pattern to the **whole** `TreeBuilder` feed path, not just one handler. Each public clinic method (`_elementtree_TreeBuilder_start`/`_data`/`_end`/`_comment`/`_pi`/`_close`) should wrap its `treebuilder_handle_*` call in a per-object critical section on `self`, so that `flush_data` + the field mutations run atomically per builder:

```c
static PyObject *
_elementtree_TreeBuilder_start_impl(TreeBuilderObject *self, PyObject *tag, PyObject *attrs)
{
    PyObject *res;
    Py_BEGIN_CRITICAL_SECTION(self);
    res = treebuilder_handle_start_lock_held(self, tag, attrs);   // handler body, no re-lock
    Py_END_CRITICAL_SECTION();
    return res;
}
```

A per-object critical section on `self` is the right mechanism (the state is per-builder). Crucially **all** feed methods must take it: the abandoned PR gh-145569 (REQ-7) locked only `treebuilder_handle_end`, which is insufficient — with `handle_start`/`handle_data`/`handle_comment`/`handle_pi` still unlocked, `self->data`/`self->last`/`self->this` continue to race. `treebuilder_flush_data`/`treebuilder_extend_element_text_or_tail` should become `_lock_held` helpers called only from inside the section.

## Notes / issue-search

Found by ThreadSanitizer fuzzing (`fusil --tsan`); vehicles `xml.etree.ElementTree` / `_elementtree` / `xml.etree.cElementTree` in fleet-03.

- **Not covered by any merged fix.** Current `main` `Modules/_elementtree.c` (last touched `7de4fcd`, gh-149571) contains **zero** `Py_BEGIN_CRITICAL_SECTION` — the feed path is still fully unlocked; the race is live on main.
- **Closest prior art: issue gh-145568 / PR gh-145569 ("Fix thread safety in `_elementtree.c`", audited for gh-116738) — CLOSED, PR never merged.** Its REQ-7 explicitly noted "`treebuilder_handle_end` reads `self->stack` and decrements `self->index` without a lock" and proposed a `_lock_held` split — i.e. it identified **one facet** (the stack/index face) of this cluster, but was abandoned unmerged and would not have fixed the `self->data`/`self->last`/`self->this` faces (it locked only `handle_end`).
- **Umbrella gh-149816 ("22 free-threading race conditions")** lists two `_elementtree` items — (69) `Element.__len__` (PR gh-149918, open) and (87) `Element.text` — both **Element-object** races, *not* the TreeBuilder feed-method race. This cluster is not in its enumerated list.
- **gh-116738 ("Audit all built-in modules for thread safety")** — squarely in remit (gh-145568 was explicitly filed under it).

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
