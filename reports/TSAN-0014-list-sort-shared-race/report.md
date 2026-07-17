# Data race: concurrent `list.sort()` of a shared `list` (`binarysort` rewrites the detached array with no critical section)

*`list_sort_impl` takes **no** per-object critical section. It detaches the items array (`ob_item = NULL`, `size = 0`) and — with no `key=` function — sets `lo.keys = saved_ob_item`, i.e. it sorts the list's **own** backing array in place via `binarysort`. Two threads sorting the same shared list (or a reader that grabbed `ob_item` just before the detach) race on the array slots.*

_AI Disclaimer: this report was drafted by Claude Code, which created and ran the reproducer; the maintainer reviewed it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Context

This is the mutate-vs-read variant of the shared-`list` thread-safety class whose write side was
fixed under **gh-129069** and whose contract is documented in **gh-142519** (list reads are lock-free
atomic item reads; "all other operations block using the per-object lock"). `list.sort()` is one of
those "other operations", but `list_sort_impl` detaches and rewrites the backing array without taking
the per-object critical section, so a concurrent lock-free reader races the in-place `binarysort`
writes — the reader-atomicity gap again, on the sort path.

## Root cause (from current-main `Objects/listobject.c`)

`list_sort_impl` detaches the backing array under **no lock**:

```c
saved_ob_size = Py_SIZE(self);
saved_ob_item = self->ob_item;    /* :2969  the list's own items array   */
saved_allocated = self->allocated;
Py_SET_SIZE(self, 0);             /* :2971  list now looks empty          */
self->ob_item = NULL;             /* :2972                                 */
self->allocated = -1;
...
lo.keys = saved_ob_item;          /* :2977  no key func -> sort IN PLACE   */
```

`binarysort` then writes `a[L] = pivot` (`:1918`) directly into `saved_ob_item` — the list's real
array. Because there is no per-object critical section, two threads can both reach this in-place
rewrite of the same array (each grabs `ob_item` in the small window before the other nulls it),
producing the `binarysort | binarysort` race; likewise a concurrent reader that loaded `ob_item`
just before the detach reads slots as `binarysort` reorders them. The observed instance stayed
crash-safe (the slots hold valid objects, just permuted), but it is an unsynchronized mutation of a
shared list's storage — unlike every other `list` mutator, which takes the object's critical section.

## Reproducer — clean plain-`list` synthetic (`repro.py`, 8/8)

It is a *sort-vs-read* race, not sort-vs-sort: a plain multi-thread `list.sort()` loop never trips it
(the sorters detach and rarely overlap), but a `sort()` racing a concurrent *reader* does — the
reader loads `ob_item` and reads slots via the lock-free `_PyList_GetItemRef*` / `_Py_TryXGetRef`
while the in-place sort rewrites the same array, a much wider window.

The one subtlety in isolating it is that `list.sort()` only does real `binarysort` work on *unsorted*
input, so the list must be re-scrambled between sorts — but a re-scramble is itself a write that
would race the readers (the TSAN-0013 class), and `halt_on_error=1` would stop on *that* instead. A
`threading.Barrier` fixes it: the re-scramble happens while the readers are **parked**, and only the
`sort()` overlaps the readers in the active phase, so the reported race is unambiguously
`binarysort`-vs-read:

```python
import sys, threading
assert not sys._is_gil_enabled(), "need --disable-gil + PYTHON_GIL=0"

SZ, ROUNDS, NR = 2000, 1500, 4
SCRAMBLED = sorted(range(SZ), key=lambda x: (x * 2654435761) & 0xFFFFFFFF)  # fixed shuffle, no `random`
L = list(SCRAMBLED)
enter, leave = threading.Barrier(NR + 1), threading.Barrier(NR + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        for _x in L:          # _PyList_GetItemRef* -> _Py_TryXGetRef(&ob_item[i])
            pass
        leave.wait()

def main_sorter():
    for _ in range(ROUNDS):
        L[:] = SCRAMBLED      # re-scramble while readers are parked at enter (no reader active)
        enter.wait()          # release readers
        L.sort()              # in-place binarysort, racing the readers
        leave.wait()

ts = [threading.Thread(target=reader) for _ in range(NR)]
for t in ts: t.start()
main_sorter()
for t in ts: t.join()
```

**Exit 66 on 8/8 runs, deterministic** (`tsan_report.txt`): `binarysort` (`listobject.c:1918`, write)
vs `_PyList_GetItemRefNoLock` → `_Py_atomic_load_ptr` (the iterator's lock-free slot read). Plain
`list`, no subclass, no `key=`. This supersedes the original wild find — a shrinkray-minimized
`email._header_value_parser.Comment` (a `list` subclass) vehicle that reproduced at ~15–30 %/run.

## Suggested fix

Take the list's per-object critical section in `list_sort_impl` around the detach + in-place sort +
reattach (or otherwise synchronize it against concurrent readers/sorters), consistent with the other
`list` mutators that are already `@critical_section` / `Py_BEGIN_CRITICAL_SECTION(self)`-guarded.

## Notes

Found by `fusil --tsan` (fleet 01). Related to **TSAN-0013** (the non-atomic *reader* faces of the
shared-list class); this is the *mutate-vs-mutate* variant.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Ruled a bug; reproduced in isolation (shrinkray); not yet individually filed.*
