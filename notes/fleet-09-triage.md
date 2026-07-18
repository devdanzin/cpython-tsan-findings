# Fleet 09 triage (2026-07-18)

`/home/fusil/runs/fusil-tsan_fleet_09` — 4 instances, 522 crash dirs (513 with a
`stdout`). **First fleet with `--tsan-weird-subclasses`** (Slice E hostile
subclasses of the target's own C types, enriched with curated diverse dunders),
**stopped early** for a matrix rebuild onto today's main (which now has the
itertools.count fix #153917).

Confirmed the flag was active: 428 `source.py` carry `_tsan_make_weird`, and the
provenance markers show **weird=3 (308 sessions), weird=2 (58), weird=1 (59)**,
weird=0 (85) — the hostile-subclass path ran across ~85% of sessions.

## Net: 0 new bugs

The catalog (now with the fleet-08 iterator mints) held almost everything:
1 new signature group, folded.

- **`dictiter_iternext_threadsafe | dictiter_iternext_threadsafe`** (1 veh,
  _hashlib) → **TSAN-0026**. A dict-iterator **self-race**: two threads both in
  `dictiter_iternext_threadsafe` (`… ← dictiter_iternextkey ← builtin_next`) —
  `next()` on a shared dict iterator. Folded (TSAN-0026 already carries the
  load/store, `dictiter_len`, and `get_index_from_order` faces).

Dedupe tally: TSAN-0006 234, TSAN-0039 96, TSAN-0037 80, TSAN-0038 72, TSAN-0026
21, then singles. noparse 4 = 1 TSAN-0033 (`_asyncio` validate_refcounts abort,
#153809) + 3 benign in-flight `session-N` (stopped early at startup). suppressed 2.

`known_races.tsv`: **131 → 132 signatures / 36 races**; fleet-09 re-ingests 0 new.

## The headline: the weird-subclass surface is SHADOWED by the iterator races

`--tsan-weird-subclasses` ran in ~85% of sessions, yet fleet-09 produced **zero**
hostile-subclass races — every crash is the same iterator/count family
(TSAN-0006/0037/0038/0039/0026). Cause: under `TSAN_OPTIONS=halt_on_error=1`,
TSan stops at the **first** race, and the op-(h) shared-iterator races (esp.
itertools.count, 234 veh) trip almost immediately — so they shadow any
slower-to-manifest race, including the weird-subclass hostile-dunder-under-C-op
class Slice E targets.

**Implication for the rebuild:** today's main has #153917, so the dominant
**count** race disappears on the rebuilt matrix. That alone removes the biggest
shadow. To actually exercise the weird-subclass (and any non-iterator) surface,
consider on the next run either:
- temporarily **suppressing the builtin-iterator races** (str/bytes/struct/dict:
  TSAN-0037/0038/0039/0026) via `--tsan-suppressions` so TSan continues past them
  to rarer races, and/or
- the upstream iterator fixes (str #153928 / struct #154013 / bytes) landing, which
  would clear them the same way #153917 cleared count.

Otherwise the shared-iterator op will keep winning the race-to-halt and the weird
subclasses won't get a look-in.

## Takeaways

- Slice E works end-to-end (weird=1–3 emitted, scripts run, no harness breakage),
  but its *findings* are masked by the far-easier iterator races under
  halt_on_error=1. Not a Slice-E problem — a scheduling-of-detection problem.
- Post-rebuild (count fixed): re-run and watch whether (a) the count slow-mode
  `long_cnt` UAF residual persists (see fleet-08 notes / TSAN-0006 OPEN ITEM), and
  (b) new surfaces (weird subclasses, non-iterator races) finally appear once the
  iterator shadow thins.
