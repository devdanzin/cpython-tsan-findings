# Data race: concurrent `list.sort()` of a shared `list` (`binarysort` rewrites the detached array with no critical section)

*`list_sort_impl` takes **no** per-object critical section. It detaches the items array (`ob_item = NULL`, `size = 0`) and — with no `key=` function — sets `lo.keys = saved_ob_item`, i.e. it sorts the list's **own** backing array in place via `binarysort`. Two threads sorting the same shared list (or a reader that grabbed `ob_item` just before the detach) race on the array slots. **Ruled a bug** by Thomas Wouters (Yhg1s, 2026-07-15) as part of the shared-builtin concurrent-access class.*

_AI Disclaimer: this report was drafted by Claude Code; root cause is from current-main source, but the isolated reproducer is not yet solved (see below)._

## Ruling

Same ruling as **TSAN-0013**: the "concurrent unsynchronized access to a shared builtin" class was
confirmed a bug by Thomas Wouters (Yhg1s, CPython RM) on 2026-07-15. This was previously *held* as an
open dev-question ("is concurrent `list.sort()` on a shared list intended to stay crash-safe?"); the
ruling answers it — yes, it's a bug.

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

## Reproduction status — honest caveat

**Not reproduced in isolation.** The race window (between `list_sort_impl` saving `ob_item` at
`:2969` and nulling it at `:2972`) is ~3 instructions, so two sorts almost never overlap inside it:
concurrent-sorter loops at 4×200k and 16×400k iterations (`repro_attempt.py`) never triggered it.
The fuzzer fleet caught it **once** (vehicle `inst-02/.../email__header_value_parser-…`, see
`tsan_report.txt`), which is what seeded this entry. The mechanism is fully grounded in the source;
only the deterministic isolated trigger is missing. (This mirrors the microscopic-window difficulty
of other detach/lazy-init races.)

## Suggested fix

Take the list's per-object critical section in `list_sort_impl` around the detach + in-place sort +
reattach (or otherwise synchronize it against concurrent readers/sorters), consistent with the other
`list` mutators that are already `@critical_section` / `Py_BEGIN_CRITICAL_SECTION(self)`-guarded.

## Notes

Found by `fusil --tsan` (fleet 01). Related to **TSAN-0013** (the non-atomic *reader* faces of the
shared-list class); this is the *mutate-vs-mutate* variant.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Ruled a bug; not yet individually filed. Isolated reproducer still open.*
