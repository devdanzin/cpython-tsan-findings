# Data race: concurrent `list.sort()` of a shared `list` (`binarysort` rewrites the detached array with no critical section)

*`list_sort_impl` takes **no** per-object critical section. It detaches the items array (`ob_item = NULL`, `size = 0`) and — with no `key=` function — sets `lo.keys = saved_ob_item`, i.e. it sorts the list's **own** backing array in place via `binarysort`. Two threads sorting the same shared list (or a reader that grabbed `ob_item` just before the detach) race on the array slots.*

_AI Disclaimer: this report was drafted by Claude Code; root cause is from current-main source, but the isolated reproducer is not yet solved (see below)._

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

## Reproduction — solved by shrinkray (sort-vs-**read**, not sort-vs-sort)

**Reproduced in isolation** (`repro.py`). The key was realizing it is a *sort-vs-read* race, not
sort-vs-sort: a plain multi-thread `list.sort()` loop never triggers it (all sorters detach and
rarely overlap), but a `sort()` racing a concurrent *reader* does — the reader loads `ob_item` and
reads slots via `list_get_item_ref`/`_Py_TryXGetRef` while the in-place sort rewrites the same
array, a much wider window.

The fleet vehicle (`inst-02/.../email__header_value_parser-…`) was minimized with **shrinkray**
(994 lines / 32.9 kB → 28 lines) with the interestingness predicate "TSan reports `in binarysort`".
It isolated the mechanism cleanly: **`email._header_value_parser.Comment` subclasses `list`**, so
sharing one instance across threads and hammering its methods runs `list.sort()` (writer)
concurrently with method/iteration reads of the same list (reader). The minimized reproducer trips
the race in **~15–30 % of single runs** (run it a few times / in a loop); the un-minimized vehicle
reproduces 100 % but is 994 lines. Confirmed report: `tsan_report_isolated.txt`.

## Suggested fix

Take the list's per-object critical section in `list_sort_impl` around the detach + in-place sort +
reattach (or otherwise synchronize it against concurrent readers/sorters), consistent with the other
`list` mutators that are already `@critical_section` / `Py_BEGIN_CRITICAL_SECTION(self)`-guarded.

## Notes

Found by `fusil --tsan` (fleet 01). Related to **TSAN-0013** (the non-atomic *reader* faces of the
shared-list class); this is the *mutate-vs-mutate* variant.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Ruled a bug; reproduced in isolation (shrinkray); not yet individually filed.*
