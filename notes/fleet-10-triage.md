# Fleet 10 triage (2026-07-18)

`/home/fusil/runs/fusil-tsan_fleet_10` — 4 instances, 170 crash dirs. **First
fleet on the rebuilt matrix** (`debug-ft-nojit-tsan` rebuilt onto main
`a1d580430c8`, which contains the itertools.count fast-mode fix #153917), still
`--tsan-weird-subclasses` and still `halt_on_error=1` (started before the
`--tsan-no-halt` work of fusil #221/#222; fleet 11 is the first `--tsan-no-halt`
fleet).

## Net: 3 new bugs + 5 new faces folded

The rebuild removed the count fast-mode shadow (#153917), and the diversity that
was hiding across sessions finally surfaced: **9 new signature groups**, resolving
to **3 genuinely new races (TSAN-0040/0041/0042, all reproduced in isolation)**
and **5 new faces of known races (folded)**.

### New races (minted, reproduced)

- **TSAN-0041 — `_elementtree` Element.extra lazy-init race (HIGH value).**
  Faces `create_extra | element_length` (2 veh) + `clear_extra | create_extra`
  (1 veh). `element_attrib_getter` (the `.attrib` getter) does an unsynchronized
  `if (!self->extra) create_extra(self, NULL)`, and `create_extra`
  (`_elementtree.c:274`) writes `self->extra = PyMem_Malloc(...)` with **no
  critical section**. Two threads first-touching a shared Element both malloc and
  write `self->extra` → write/write race + a leaked `ElementObjectExtra`, and the
  write also races readers (`element_length`) / `clear_extra`. A **realistically
  shared object** (a parsed tree handed to worker threads), so higher real-world
  priority than the iterator races. Isolated repro (shared Element + concurrent
  `.attrib`/`len()`) → exit 66. **Upstream candidate — awaiting go-ahead.**

- **TSAN-0040 — set iterator shared-cursor race** (6 veh, the fleet headline by
  count). `setiter_iternext | setiter_len`: `setiter_len` (`setobject.c:1063`,
  via `operator.length_hint`) reads the shared iterator's countdown/index while
  `setiter_iternext` advances it. The **set** sibling of the builtin-iterator
  cursor family (TSAN-0037 bytes / TSAN-0038 str / TSAN-0039 struct / TSAN-0026
  dict). Isolated repro (shared `iter(set(...))` + next()/length_hint) → exit 66.

- **TSAN-0042 — shared `itertools.groupby` race** (1 veh). `groupby_next`
  (`itertoolsmodule.c:~537`) mutates the groupby's cross-call state
  (`currkey/currvalue/tgtkey/currgrouper`) with no per-object lock; a shared
  groupby driven from several threads races (`groupby_next` self-race in the
  fleet; `_grouper_create | groupby_next` / `_grouper_next` in the repro). The
  shared stateful-itertools-object class (like count / TSAN-0006). Isolated repro
  (shared `groupby(range(...))` + concurrent `list()`) → exit 66.

### Folded faces (known races, new signature variants)

- **`dictiter_iternext_threadsafe | dictiter_iternextkey`** (3 veh) → **TSAN-0026**
  (dict-iter; `iternextkey` is just another advance-side face).
- **`unicodeiter_next | unicodeiter_next`** + **`unicodeiter_len | unicodeiter_next`**
  (2 veh) → **TSAN-0038**. The **general (non-ASCII) unicode iterator** — same
  `it_index`/`it_seq` non-atomicity as the ASCII fast path (`unicode_ascii_iter_*`);
  same bug/#153928, the other code path. TSAN-0038 widened to cover both.
- **`_decimal_Context_clear_traps_impl | type_call`** (1 veh) → **TSAN-0019**
  (shared `_decimal` Context race; `clear_traps_impl` writer is a TSAN-0019 side,
  the `type_call` reader is just the Context constructor touching the shared ctx).
- **`_PyLong_DigitCount | _PyMem_DebugRawAlloc`** (1 veh) → **TSAN-0006**. The
  count **slow-mode use-after-free** residual of #153917: `count_repr` borrows
  `lz->long_cnt` and `PyObject_Repr`s it (→ `_PyLong_DigitCount` /
  `long_to_decimal_string`) while `count_nextlong` replaces it and the `next()`
  caller frees the old int, whose storage the allocator then reuses. Generic-
  shaped face — count_repr confirmed on the stack (read frame #8, `count_repr:3696`).

## Correction: the `_decimal Context` race was never new

Earlier notes flagged `_decimal_Context_clear_traps_impl | context_repr` as a
possible new find "to pin down." It is **already TSAN-0019** (one of its 9 faces).
The `tsanNEW` label in the multi-race experiment only meant "no catalog was
loaded," not a real gap. Resolved.

## Dedupe tally

TSAN-0026 23, TSAN-0038 22, TSAN-0006 21, TSAN-0037 20, TSAN-0039 20, **TSAN-0040
6**, TSAN-0013 6, TSAN-0007 5, TSAN-0019 3, TSAN-0010 3, then singles.
other: suppressed 12, noparse 11 (in-flight `session-N` + benign). new-signature
groups: 9 → all resolved (3 minted + 6 folded signature variants across 4 ids).

`known_races.tsv`: **132 → 145 signatures / 36 → 39 races**; fleet-10 re-ingests
**0 new**.

## Why fleet 10 finally showed diversity (and the case for `--tsan-no-halt`)

Fleet 09's headline was that the iterator/count races **shadow** everything under
`halt_on_error=1`. Rebuilding onto main (#153917) killed the count *fast-mode*
shadow — but count *slow-mode* (TSAN-0006, 21 veh) and the other iterator races
still trip first and mask the rest **per session**. The diversity here is still
**across** sessions, not within them. This fleet is exactly the motivation for the
multi-race work (fusil #221/#222): **fleet 11 runs with `--tsan-no-halt`**, so each
session should surface its whole race set (and drop a `tsan_races.tsv` sidecar the
updated `ingest.py` reads), which should finally expose the weird-subclass surface
Slice E targets instead of leaving it shadowed.

## Upstream candidates (awaiting maintainer go-ahead — outward-facing)

1. **TSAN-0041 `_elementtree` extra lazy-init** — the strongest new finding
   (C accelerator, shared-object, leak + torn pointer, realistic sharing).
2. **TSAN-0040 set iterator** — fits the builtin-iterator family (umbrella #153852
   / alongside #153928 / #154013).
3. **TSAN-0042 groupby** — shared stateful itertools object (alongside count
   #153908).
