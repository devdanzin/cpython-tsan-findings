# Fleet 12 triage (2026-07-19) — second `--tsan-no-halt` fleet

`/home/fusil/runs/fusil-tsan_fleet_12` — 4 instances, 270 crash dirs, **107 with a `tsan_races.tsv`
sidecar**. Second `--tsan-no-halt` fleet, same rebuilt matrix (`main@a1d580430c8`).

## Multi-race value (holding steady)

270 dirs → **392 race instances** (avg 1.45/dir), **70 distinct signatures** captured vs the **51** a
`halt_on_error=1` fleet would see → **122 instances masked before, 19 signatures never a first race**.
Consistent with fleet-11 (~37% masked). Our recent mints all dedupe cleanly now: TSAN-0040 (15 veh),
TSAN-0041 (5), TSAN-0044 (5), TSAN-0042 (1), **TSAN-0045 (1)** — the GenericAlias crash we just filed
as cpython#154043.

## Net: 0 new fileable races — the catalog + upstream coverage is converging

8 new signature groups → **3 cataloged for dedup (2 already-filed upstream, 1 value-benign-new)**,
1 folded, and 4 documented-but-uncataloged (all known cascades/artifacts or a flagged unknown). No
new *fileable* bug — a healthy sign that fleets are now mostly re-finding known races.

### Cataloged (minted for dedup)

- **TSAN-0046 — `io.IncrementalNewlineDecoder` state race** = **cpython#144777 (CLOSED)**. `.reset()`
  writes `self->seennl` (`textio.c:630`) unlocked vs `.newlines` reading it. Value-benign.
  Reproduced in isolation. Already filed.
- **TSAN-0047 — `locale.localeconv()` heap-use-after-free** = **cpython#127081 (OPEN, "Thread-unsafe
  libc functions")**. Concurrent `localeconv()` calls race the single static `struct lconv` the C
  library returns → UAF of its strdup'd string fields. Memory-unsafe but libc-rooted; the fix is
  CPython-side locking. Not reproduced in isolation (needs a locale with non-empty monetary fields;
  the C-locale default has none). Already tracked upstream.
- **TSAN-0048 — `csv.reader` `line_num` race (NEW).** `Reader_iternext` writes `self->line_num`
  (`_csv.c`) vs a concurrent `reader.line_num` member read. Appears unfiled, but **value-benign** (a
  stale counter, no crash) → low priority; not proposing a filing. Reproduced in isolation.

### Folded

- `_PyLong_DigitCount | _PyMem_DebugRawFree` → **TSAN-0006** (count slow-mode UAF face; the `…Free`
  sibling of the `…Alloc` face already cataloged).

### Documented, left uncataloged (4 — re-ingest's remaining "new")

- **`SEGV addr=0x? pc=0x…4b6c`** (2 veh) — the **count-UAF cascade** (TSAN-0006's fatal downstream),
  identical pc to fleet-11. Unsymbolized + build-specific pc, so not folded.
- **`_Py_atomic_load_ssize_relaxed | _Py_atomic_load_ssize_relaxed`** (2 veh) — **TSAN-0026** dict
  iterator, collapsed to `atomic | atomic` by the parser (both innermost frames are the atomic
  accessor). Same as fleet-11; the fix is the planned `tsan_dedup` pass to skip `_Py_atomic_*` frames
  (cross-repo contract change, deferred).
- **`Modules/socketmodule.c:sock_accept_impl | sock_finalize`** (1 veh) — **FLAGGED for analysis.**
  One thread is in `sock_accept_impl:3056` while another runs `sock_finalize:5544` reached via a
  dealloc chain (`… BaseException_dealloc → tb_dealloc → frame_dealloc → subtype_dealloc →
  PyObject_CallFinalizer`). i.e. a socket is being *finalized/deallocated* while another thread is
  mid-`accept()` on it. That is either a real free-threading lifetime bug (socket destroyed while a
  method runs on it — normally the bound call keeps `self` alive) or a fuzzer generated-code artifact
  (a shared socket whose last ref is dropped during exception cleanup while another thread uses it
  without its own ref). Not confidently attributable from 1 vehicle; **worth a deeper look if it
  recurs.**
- **`SEGV addr=0x? pc=0x…4418`** (1 veh) — an unsymbolized SEGV in an `_lsprof` vehicle
  (`_lsprof-cpu_load-segfault`); most likely the lsprof-state (TSAN-0008) cascade. Build-specific pc,
  not folded.

## Catalog

`known_races.tsv`: **159 → 163 signatures / 42 → 45 races** (3 mints + the count `…Free` fold).
Fleet-12 re-ingests to **4** (the documented leftovers above). Dedupe tally led by TSAN-0006 (78),
TSAN-0039 (59), TSAN-0038 (51), TSAN-0037 (43), TSAN-0026 (36), TSAN-0040 (15).

## Upstream posture

Nothing new to file from this fleet. `locale.localeconv()` (TSAN-0047 / #127081) and the
`io.IncrementalNewlineDecoder` race (TSAN-0046 / #144777) are already tracked; the `csv.reader`
`line_num` race (TSAN-0048) is value-benign and not worth a filing. The **socket `accept` vs
`finalize`** race is the one open question — flagged for deeper analysis if it recurs in a later
fleet.
