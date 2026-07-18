# Fleet 08 triage (2026-07-18)

`/home/fusil/runs/fusil-tsan_fleet_08` — 4 instances, 417 crash dirs (409 with a
`stdout`). **First fleet run with Phase-4 A–E** (worker roles + provenance +
richer target objects + extension-object iterators; **`--tsan-weird-subclasses`
NOT passed**). It crashes *much* faster than fleet 07 (pre-Phase-4): the op-(h)
shared-iterator machinery + the Slice-A `length_hint` reader lit up the whole
**builtin/stdlib iterator-cursor race family** at once.

## Net: 0 new-and-unfiled bugs

All 10 new signature groups are the iterator/count shared-cursor race class —
every one maps to an existing catalog race or a known upstream issue. 2 new
catalog ids were minted for the str and struct iterators (both already filed
upstream) so fleets dedupe them.

## The 10 groups

| # | signature | veh | disposition |
|---|---|---:|---|
| 1 | `count_nextlong \| count_repr` | 142 | **TSAN-0006** (itertools count, slow/big-int mode) |
| 2 | `unpackiter_iternext \| unpackiter_len` | 64 | **TSAN-0039** minted (struct, cpython#154013) |
| 3 | `striter_len \| striter_next` | 60 | **TSAN-0037** (bytes iter, length_hint face) |
| 4 | `unicode_ascii_iter_next \| unicodeiter_len` | 40 | **TSAN-0038** minted (str, cpython#153928) |
| 5 | `unpackiter_iternext \| unpackiter_iternext` | 24 | **TSAN-0039** (struct self-race) |
| 6 | `unicode_ascii_iter_next \| unicode_ascii_iter_next` | 15 | **TSAN-0038** (str self-race) |
| 7 | `_Py_TYPE_impl \| _PyMem_DebugRawAlloc` | 7 | **TSAN-0006** (count slow-mode **UAF** face) |
| 8 | `_Py_TYPE_impl \| _PyMem_DebugRawFree` | 1 | **TSAN-0006** (count UAF face) |
| 9 | `dictiter_iternext_threadsafe \| dictiter_len` | 1 | **TSAN-0026** (dict iter, len face) |
| 10 | `long_to_decimal_string_internal \| _PyMem_DebugRawFree` | 1 | **TSAN-0006** (count UAF face) |

## itertools.count (TSAN-0006) — the big story, incl. a UAF escalation

op-h shares `count(10**18, 2)` = **big-int slow mode** (`lz->long_cnt`, a
`PyObject*`, not the fast `lz->cnt` ssize). Two faces:

- **Field race** (#1, 142 veh): `count_repr` plain-reads `lz->long_cnt` while
  `count_nextlong` writes it — `count_nextlong | count_repr`.
- **Use-after-free** (#7/#8/#10, 9 veh): `count_repr` does
  `PyUnicode_FromFormat("count(%R)", lz->long_cnt)` — a **borrowed** read — and
  reprs it; `count_nextlong` writes `lz->long_cnt = stepped_up` and **returns the
  old int** (ref transferred to the `next()` caller, which then DECREFs → frees).
  So `count_repr`'s `PyObject_Repr(old_long_cnt)` reads a freed int
  (`_Py_TYPE_impl` / `long_to_decimal_string_internal` vs `_PyMem_DebugRaw*`).
  All three have `count_repr` on the stack (frame ~#6).

TSAN-0006 is **FIXED upstream** (#153908 / PR #153917), and the **fleet build has
no `_Py_atomic` in `count`** → it predates the fix, so this is the pre-fix state.
**OPEN ITEM:** #153917 hardened the fast-mode `lz->cnt` (+ a critical section for
the fast→slow transition); re-check on a **post-#153917 build** whether
`count_repr`/`count_nextlong`'s **slow-mode `long_cnt`** access is under a critical
section. If not, the slow-mode race + UAF is an **incomplete-fix residual** worth
a note on #153908. (Caveat: the 3 UAF-face signatures are generic-shaped — a
future non-count repr-UAF would mislabel as TSAN-0006; verify `count_repr` on the
stack.)

## New catalog entries (both known upstream — cataloged for dedup)

- **TSAN-0038** — str iterator `unicode_ascii_iter_next` it_index race
  (:14983 write vs `unicodeiter_len`:14997 read) = **cpython#153928** (johng's;
  reproduced + commented by us). TSAN-0037's notes predicted it ("str did NOT trip
  fleet-06, bytes dominated"); with the Slice-A length_hint reader it now trips
  (55 veh).
- **TSAN-0039** — struct `unpackiter_iternext` index race (:2278 write vs
  `unpackiter_len`:2249 read) = **cpython#154013** (88 veh). First fleet capture.

Both fold the bytes (TSAN-0037) / dict (TSAN-0026) siblings into one family:
str/bytes/struct/dict/count builtin iterators all mutate per-iteration cursor
state with no per-object critical section (the Yhg1s shared-builtin class).

## noparse (5), suppressed (0)

Small: 5 benign/in-flight. Almost everything parsed to a race this run — the
iterator races dominate and TSan halts on them fast.

## Catalog

`known_races.tsv`: **121 → 131 signatures / 34 → 36 races**
(TSAN-0006 +4, TSAN-0037 +1, TSAN-0026 +1, TSAN-0038 +2, TSAN-0039 +2). Fleet-08
re-ingests **0 new groups**.

## Takeaways

- Phase-4's op-(h) shared-iterator sharing + the `length_hint` reader is a **very**
  productive iterator-cursor-race driver — it reproduced str/bytes/struct/dict/count
  in one short fleet (that's the "much faster crashes"). All known; the value is
  coverage + the count slow-mode UAF characterization.
- **Move the fleet to a post-#153917 build** to (a) stop the 142-veh count noise and
  (b) settle the slow-mode-`long_cnt` residual question.
- Fleet 09 will add `--tsan-weird-subclasses` (the hostile-subclass path), a
  different surface from these iterator races.
