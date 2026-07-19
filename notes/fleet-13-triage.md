# Fleet 13 triage (2026-07-19) — 0 new, stopped for the un-masking experiment

`/home/fusil/runs/fusil-tsan_fleet_13` — 4 instances, 170 crash dirs (47 sidecars). A third standard
`--tsan-no-halt` fleet, stopped early to free the box for the first **gateway-un-masking** fleet.

## Net: 0 new races (convergence, as fleet 12 predicted)

4 new signature groups, all 1 veh — every one a known face or cascade:

- `_PyLong_DigitCount | _PyLong_InitTag` → **TSAN-0006** (count slow-mode; count_repr/count_nextlong
  confirmed on the stack — the big-int build path racing count's long_cnt).
- `Objects/bytesobject.c:striter_len | striter_len` → **TSAN-0037** (bytes-iter `length_hint`
  self-race face).
- `Modules/itertoolsmodule.c:groupby_next | groupby_step` → **TSAN-0042** (`groupby_step` is the
  inner helper `groupby_next` calls at `:564`; same shared-groupby race).
- `SEGV addr=0x? pc=0x…4b6c` → the **count-UAF cascade** (TSAN-0006's fatal downstream), same
  build-specific pc as fleets 11/12; left uncataloged.

`prioritize.py` confirms nothing crash-shaped is new (the only CRASH-scored signature is the known
count-cascade SEGV). `known_races.tsv`: 163 → **166 signatures / 45 races** (3 folds); re-ingests to
**1** (the count SEGV). Dedupe tally led by TSAN-0006 (52), TSAN-0026 (33), TSAN-0038 (30),
TSAN-0037 (27), TSAN-0039 (27) — the five gateways, i.e. exactly what the next fleet suppresses.

## Takeaway

Three consecutive standard `--tsan-no-halt` fleets (11 → 12 → 13) have now gone
3-new → 0-new-fileable → 0-new: the gateways dominate and the tail is exhausted at this
configuration. The **gateway-un-masking fleet** (now running) is the right next step to reach the
shadowed surface (weird-subclasses, rare crashers).
