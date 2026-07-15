# Data race: non-atomic list readers race `list_resize`'s atomic publish on a shared `list`

*On a free-threaded build, `list.append`/`pop` grow a list via `list_resize`, which publishes the new `ob_item` pointer and `ob_size` with **atomic** stores. But many list *readers* load those fields with **plain, non-atomic** access — `Py_SIZE` in tuple-unpack, `PyList_GET_ITEM` in `b"".join`, `marshal.dumps`, and others. Two threads doing `read` and `append` on the same shared list therefore race. It is value-benign on aligned hardware, but it is a genuine data race — and **CPython considers it a bug** (Thomas Wouters / Yhg1s, 2026-07-15).*

_AI Disclaimer: this report was drafted by Claude Code, which created and ran the reproducers; the maintainer reviewed it._

## Ruling

This class was floated to the CPython maintainers by sending `shared_list_race.py` and
`notes/shared-builtin-concurrent-access.md` verbatim. **Thomas Wouters (Yhg1s, CPython release
manager) replied: "Yes, that's a bug."** So this is a valid free-threading defect — the non-atomic
reader should use the same atomic accessor the writer uses — not "don't share it" behaviour. The
catalog previously *suppressed* this class as an assumed non-bug; that suppression has been removed.

## Summary

`list_resize` (`Objects/listobject.c:165`) publishes the reallocated backing store atomically:

```c
_Py_atomic_store_ptr_release(&self->ob_item, &array->ob_item);   /* new items array   */
self->allocated = new_allocated;
Py_SET_SIZE(self, newsize);                                      /* atomic ob_size store */
```

Readers, however, use the plain macros:

- **`Py_SIZE` / `_Py_SIZE_impl`** (`Include/object.h`) — e.g. `UNPACK_SEQUENCE`'s fast path reads the size non-atomically.
- **`stringlib_bytes_join`** (`Objects/stringlib/join.h:63`) — `b"".join(list)` reads items via `PySequence_Fast_GET_ITEM` / `PyList_GET_ITEM`, a plain load of `ob_item`.
- **`w_complex_object`** (`Python/marshal.c`, cataloged separately as **TSAN-0010**) — `marshal.dumps(list)` reads `ob_item[i]` plainly.

Any of these run concurrently with `append`/`pop` on the same shared list → an atomic-store-vs-plain-read data race on `ob_item`/`ob_size`.

## Reproducers (both confirmed, exit 66)

- **`repro_size.py`** — the `Py_SIZE` face: one thread unpacks (`a, b, c = shared`), another `append`/`pop`s.
  Signature `Include/object.h:_Py_SIZE_impl | …` (see `tsan_report_size.txt`).
- **`repro_join.py`** — the `bytes_join` face: one thread `b"".join(shared)`, another `append`/`pop`s.
  Signature `…:_Py_atomic_store_ptr_release | Objects/stringlib/join.h:stringlib_bytes_join` (see `tsan_report_join.txt`).

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro_join.py     # -> WARNING: ThreadSanitizer: data race, exit 66
```

## Root cause

The writer side of `list` was hardened for free-threading (atomic publish of `ob_item`/`ob_size`,
per-object critical sections on the mutating methods), but the many *reader* call sites that use the
raw `PyList_GET_ITEM` / `Py_SIZE` macros were not converted to the atomic accessors. So a reader can
observe `ob_item`/`ob_size` mid-update. On x86-64/aarch64 an aligned word load/store won't tear, so
the observed races are value-benign and crash-free — but they are C11 data races (UB), and the
free-threading contract is that operations on builtin containers are data-race-free, so they are
bugs to fix.

## Impact / severity

Low individually (value-benign, no crash observed), but **systemic** — it is a whole class of reader
call sites, not one bug, and it is trivially triggered by any program that reads a shared list from
one thread while another mutates it (which the free-threading model is supposed to make safe).

## Suggested fix

Convert the list read sites to the atomic accessors the writer already uses:

- item reads → `_PyList_GetItemRef` (the QSBR-safe accessor) instead of raw `PyList_GET_ITEM`;
- size reads on the fast paths → an atomic load of `ob_size` instead of plain `Py_SIZE`.

This is squarely within **gh-116738** ("Audit all built-in modules for thread safety") — an audit of
builtin-container *reader* sites for non-atomic access under free-threading.

## Notes

Found by `fusil --tsan` (fleet 01). Formerly suppressed as an assumed non-bug; un-suppressed after
the Yhg1s ruling. The `Py_SIZE`, `bytes_join`, and `marshal` (**TSAN-0010**) faces are the same
underlying defect at different reader sites; **TSAN-0014** (concurrent `list.sort()`) is the closely
related mutate-vs-mutate variant.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Ruled a bug by CPython; not yet individually filed.*
