# Fleet 06 triage (fusil-tsan_fleet_06, 2026-07-18)

4 instances, no restart, **223 crash dirs** (stopped early to look at results). **First fleet with
the P3.1 shared-iterator + read-while-mutate op-mix (fusil PR #211).** The new ops paid off
immediately: the dominant signature is a brand-new shared-iterator-cursor race.

## Result

223 dirs → **8 known races + 2 new signature groups**, and both new groups are the
**shared-iterator-cursor race class the new ops were built to reach**:

- **[196] `Objects/bytesobject.c:striter_next | (self)` → MINTED TSAN-0037.** The **bytes iterator**
  advances `it->it_index` non-atomically: read at the bounds check (`bytesobject.c:3446`) vs
  read+increment `seq->ob_sval[it->it_index++]` (`:3448`), when one `bytes` iterator is shared across
  threads → data race + out-of-bounds read; plus the `it->it_seq = NULL; Py_DECREF(seq)` exhaustion
  branch double-DECREFs the shared bytes object. **This is the exact bytes analog of the str-iterator
  race cpython#153928** (`unicode_ascii_iter_next`). Reproduced deterministically (exit 66,
  `reports/TSAN-0037-*/repro.py`, 8 threads draining one shared `iter(b"A"*2000)`); matches all 196
  vehicles. Surfaced by op (h) (`iter(b"A"*4096)` shared by reference).
- **[3] `_Py_atomic_load_ssize_relaxed | dictiter_iternextitem_lock_held` → folded to TSAN-0026.**
  Two threads advancing ONE shared **dict iterator** race in the `dictiter_iternext_threadsafe`
  machinery (lock-free size read at `dictobject.c:6082` vs the lock_held path at `:6017`) — the dict
  analog of the bytes/str shared-iterator cursor race. Distinct *mechanism* from TSAN-0026's `ma_values`
  face, but the same `dictiter_iternext_threadsafe` home, so folded there (signature added). Surfaced by
  op (h) `iter({...})` / op (i) `list(_bag.items())`.

Known-race tally (the always-there ones, small since the run was short): TSAN-0013 (5), 0005 (3),
0007 (3), 0006 (2), 0008/0010/0012/0030 (1 each). suppressed 2; noparse 5 (1 = the known
`_Py_REFCNT(op)>0` FT refcount-race abort face, 4 clean/exit noise).

Note: the **str** iterator (`unicode_ascii_iter_next`, #153928) did **not** trip this run (0 dirs) —
bytes won the race lottery at 196/223. Same code shape, so it would appear given more time.

## Catalog changes (this triage)

- **TSAN-0037** minted (`reports/TSAN-0037-bytes-iterator-shared-race/` — meta.json + repro.py +
  backtrace.txt). `related_issues: cpython#153928`. Candidate to add to the #153928 thread as "the
  same race exists in `bytesobject.c:striter_next`" rather than a separate filing.
- **TSAN-0026** +1 signature (dict shared-cursor face) + note.
- `known_races.tsv` regenerated: 117 → 119 signatures / 34 races. Re-ingest → **0 new groups**.

## Takeaway

The op-mix enrichment (PR #211) works: one short fleet immediately surfaced the shared-iterator race
class (bytes + dict), which the entire pre-#211 fleet history (01–05) never reached. Fleet 07 should
keep running the new ops — the str/list/tuple/range iterator faces and the `it_seq` double-DECREF UAF
(vs the plain `it_index` race TSan halts on first) are still likely to appear.
