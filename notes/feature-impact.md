# Feature impact on the `--tsan` campaign (fleets 01–12)

Which fusil `--tsan` features unlocked which findings, and what the multi-race data says about
where the remaining bugs are hiding. Built from `reports/*/meta.json` (`found_in`) plus a
re-analysis of the fleet-11/12 `tsan_races.tsv` sidecars (2026-07-19).

## First-found attribution: feature → races

Each catalogued race, mapped to the fleet (and the feature that shipped just before it) that first
surfaced it:

| Era / feature shipped | Fleets | New race ids | Count | What it unlocked |
|---|---|---|---|---|
| Base `--tsan` stress region (Phase 1–2) | F01–02 | TSAN-0001…0030 | **30** | The standing harvest — every shared-object race already reachable once the concurrency-stress region existed. Big because the catalog started empty. |
| dedup + op-mix enrichment (Phase 2–3) | F03–04 | TSAN-0031…0036 | 6 | Diminishing returns as the catalog filled; a few module-specific races (elementtree, bufferedio, socket-timeout, instrumentation). |
| **op-h shared-iterator + `length_hint` reader** (Phase 3.1/4) | F06, F08 | TSAN-0037/0038/0039 | 3 | The **builtin-iterator cursor family** — bytes (F06), then str + struct (F08). Directly caused by sharing one iterator and reading its cursor via `operator.length_hint`. |
| **matrix rebuild** (count fast-mode fix removed the dominant shadow) | F10 | TSAN-0040/0041/0042 | 3 | set iterator, **`_elementtree` extra lazy-init**, **`itertools.groupby`** — races that had been shadowed by the count fast-mode race until #153917 landed. |
| **`--tsan-no-halt`** (multi-race per session, fusil #221/#222) | F11–12 | TSAN-0043…0048 | 6 | The **per-session masked** findings: descriptor `__qualname__`, the **GenericAlias iterator crash**, generic seq/deque iterators, `io` newline decoder, `localeconv`, `csv` reader. |

## The single most important data point

**TSAN-0045 — the `types.GenericAlias` iterator double-DECREF that we filed as cpython#154043 and
confirmed segfaults on plain free-threaded builds — is NEVER a first race.** In the fleet-11/12
sidecars it appears only *after* another race in the session (2/2 times, always after the struct
iterator TSAN-0039). Under `halt_on_error=1` it was structurally impossible to observe: TSan would
have stopped at the first race every time. It took `--tsan-no-halt` to see it at all. A filed,
reproducible *crash* that no prior fleet could have found is the clearest possible justification for
the multi-race work.

## Multi-race value, quantified (F11 + F12)

| fleet | dirs | race instances | distinct sigs captured | sigs a `halt=1` fleet would see (first-only) | instances masked | sigs never-first |
|---|---|---|---|---|---|---|
| F11 | 223 | 344 | 62 | 41 | **129 (~37%)** | 21 |
| F12 | 270 | 392 | 70 | 51 | **122 (~31%)** | 19 |

Roughly a third of all races per fleet, and ~20 distinct signatures each, are invisible to a
first-race-only run.

## Where the remaining bugs hide: gateway vs masked races

Ranking the fleet-11/12 races by how often they are the **first** race in a session (i.e. the
"shadow" a `halt=1` run would report):

**Gateways (dominate the first-race slot):**

| race | first-race count | total appearances |
|---|---|---|
| TSAN-0006 (count) | 96 | 191 |
| TSAN-0039 (struct iter) | 75 | 107 |
| TSAN-0026 (dict iter) | 62 | 72 |
| TSAN-0038 (str iter) | 54 | 82 |
| TSAN-0037 (bytes iter) | 42 | 76 |

Five signatures account for nearly every first race. They are all either known-and-filed
(count = #153908/#153981) or value-benign-per-strategy (the iterator cursors, gh-124397). They add
almost nothing new now, but they crowd out the tail.

**Carriers:** the rarer *new* races cluster behind specific gateways — TSAN-0044 (seq iter) followed
TSAN-0039 (struct iter) in 7/9 sessions; TSAN-0045 (the ga crash) followed it 2/2. So op-h sessions
that exercise diverse iterators are the productive ones for the rare iterator-family tail.

## Implications / next steps

1. **Un-mask by suppressing the gateways at the TSan level** (`catalog/gateway_suppressions.txt`).
   Even with `--tsan-no-halt`, the five gateway races dominate vehicle counts and drown the tail.
   Feeding them to `TSAN_OPTIONS=suppressions=…` for a dedicated fleet stops TSan reporting them at
   all, so the rarer races become first-class and accumulate vehicles — which should finally expose
   the **weird-subclass surface** (Slice E, flagged shadowed in the fleet-09 note) and raise the
   vehicle count on rare crashers enough to minimize them. See `fleet/README.md` "un-masking".
2. **Prioritize rare + crash-shaped signatures for the "replay without a sanitizer" check**
   (`scripts/prioritize.py`). TSAN-0045 went from a TSan warning to a filed crash purely by
   re-running its vehicle on a plain FT build; automating that triage catches the next one.
3. **Guard against elided-middle races** — for F11/F12 the bounded stdout read missed nothing
   (all stdouts fit within head+tail), but a future runaway-stdout fleet could hide races in the
   elided middle. `scripts/prioritize.py` re-parses the full stdout when it exceeds the bound.
