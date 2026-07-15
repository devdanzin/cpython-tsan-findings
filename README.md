# cpython-tsan-findings

Triage + dedupe catalog for **ThreadSanitizer data races** found by fusil's `--tsan` mode in
free-threaded CPython (and FT-unsafe C extensions). The TSan analogue of the sibling
`cpython-oom-findings` catalog.

## Layout

```
reports/TSAN-NNNN-<slug>/      one directory per confirmed race
  meta.json                    id, title, signatures[], sites[], status, upstream_issue
  report.md                    writeup
  tsan_report.txt              the raw `WARNING: ThreadSanitizer: data race` report
  repro.py                     minimal reproducer (run under a --disable-gil --with-thread-sanitizer build)
catalog/
  known_races.tsv              <race_id>\t<signature> dedupe snapshot (generated)
  suppressions.txt             local/per-target TSan suppressions (+ pointer to CPython's)
scripts/
  gen_known_races.py           reports/*/meta.json -> catalog/known_races.tsv
  ingest.py                    batch-dedupe a pile of --tsan crash dirs vs the snapshot
```

## The race signature

A race is keyed by the **unordered pair of its two racing access sites** (`file:func | file:func`,
sorted), taken as the innermost CPython source frame of each access stanza that isn't generic
call/eval plumbing. Function-level (not line) so it's stable across builds. This is exactly what
fusil's `fusil/python/tsan_dedup.py` computes — the catalog reuses that one parser (loaded by
file path) so the snapshot and the in-loop deduper can never drift.

## Workflow

```bash
# 1. dedupe a batch of --tsan crash dirs; new signatures need a report:
FUSIL_TSAN_DEDUP=../fusil/fusil/python/tsan_dedup.py python3 scripts/ingest.py ~/crashers/tsan-*/*
# 2. write reports/TSAN-NNNN-.../meta.json (paste its signatures[] from ingest)
# 3. regenerate the snapshot fuzzers/instances read:
python3 scripts/gen_known_races.py
# 4. point fusil at it:  fusil-python-threaded --tsan --tsan-dedup-catalog catalog/known_races.tsv --tsan-dedup-prune
```

Nothing is catalogued yet — Phase 2 of `--tsan` just stood the repo up. `suppressions_free_threading.txt`
in CPython is currently **empty**, so races in core are genuine findings, not pre-suppressed.
