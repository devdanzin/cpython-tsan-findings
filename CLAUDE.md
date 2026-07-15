# CLAUDE.md — cpython-tsan-findings

Sibling of `cpython-oom-findings`, for **ThreadSanitizer data races** from fusil's `--tsan` mode.

## What this repo is

A read-only-by-instances dedupe catalog. Fuzzer instances load `catalog/known_races.tsv` (via
`fusil --tsan-dedup-catalog`) to label/prune duplicate races in-loop; the single writer (triage)
regenerates it from `reports/*/meta.json`. No shared *mutable* catalog → no concurrent-write problem.

## The signature is fusil's, not ours

`fusil/python/tsan_dedup.py` is the ONE source of truth for the race signature (the sorted pair of
top-real-CPython sites from the two TSan access stanzas). `scripts/ingest.py` loads it **by file
path** (`FUSIL_TSAN_DEDUP=…/fusil/python/tsan_dedup.py`, default `../fusil/…`) rather than
re-implementing it, so the snapshot and the in-loop deduper can't drift. (Contrast the OOM catalog,
which vendors its parsing and stays in lockstep by hand — TSan stanza parsing is fiddlier, so we
share the code instead.)

## Conventions (mirror cpython-oom-findings)

- Ids: `TSAN-0001`, `TSAN-0002`, … `meta.json` carries `id`, `title`, `signatures` (one or more —
  a race can present slightly different site pairs across runs), `sites` (human `file func:line`),
  `status` (`drafted`/`gisted`/`reported`/`fixed`/`folded`), `upstream_issue`.
- `status: folded` rows are skipped by `gen_known_races.py` (merged into another id which carries
  the signature).
- A race whose both sites are thread/frame scaffolding (`_threadmodule.c`/`thread_pthread.h`) is
  **framework noise** (fusil labels it `tsanFRAME`), not a target finding — don't file it.
- Build: `~/projects/python_build_matrix/builds/debug-ft-nojit-tsan` (CPython 3.16
  `--disable-gil --with-thread-sanitizer`). Symbolize with `DEBUGINFOD_URLS=` (the Ubuntu
  debuginfod server is blackholed and hangs llvm-symbolizer otherwise).
- Outward-facing steps (filing upstream, gists) need the maintainer's go-ahead, same as the OOM
  catalog.

## Commands

```bash
python3 scripts/gen_known_races.py                                   # reports -> known_races.tsv
FUSIL_TSAN_DEDUP=../fusil/fusil/python/tsan_dedup.py \
  python3 scripts/ingest.py ~/crashers/tsan-*/*                      # batch dedupe
```
