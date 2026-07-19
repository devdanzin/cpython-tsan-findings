# Fleet 11 triage (2026-07-18) — the first `--tsan-no-halt` fleet

`/home/fusil/runs/fusil-tsan_fleet_11` — 4 instances, 223 crash dirs. **First fleet with
`--tsan-no-halt`** (TSan `halt_on_error=0`), so each session captures *every* race it hits, not
just the first, and multi-race kept dirs drop a `tsan_races.tsv` sidecar (fusil #222). Same rebuilt
matrix (`main@a1d580430c8`), still `--tsan-weird-subclasses`.

## The headline: what multiple-races-per-session buys us

The `--tsan-no-halt` path fired in production — **82 / 223 dirs carried a `tsan_races.tsv`
sidecar** (the ≥2-race sessions), and the ingest tallied the full picture:

| metric | value |
|---|---|
| dirs | 223 |
| **total race instances captured** | **344** (avg **1.54**/dir, max **8**) |
| distinct signatures captured | 62 |
| distinct signatures a `halt_on_error=1` fleet would have seen (first race only) | 41 |
| **race instances masked under halt=1** (order ≥ 1) | **129** (~37% of all races) |
| **distinct signatures that NEVER appeared as a first race** (only-via-multi-race) | **21** |

So over a third of the races in this fleet — and 21 whole signatures — were **invisible to every
prior fleet**. Three genuinely-new races (below) are among the only-via-multi-race set: they had
been shadowed in fleets 06–10 by the dominant `count` / bytes / str / struct races. The
`after_fault` flag correctly stayed 0 everywhere (the cascade SEGVs land at the *end* of each
stream, so nothing was mislabeled as a corruption artifact).

**This is the payoff of #221/#222.** halt_on_error=1 gave one race per session; no-halt gives the
whole set, and the deduper/ingest fold + surface it. Prior fleets found ~1 new race per ~200 dirs;
fleet-11 surfaced **3 new races + 5 folded faces from 223 dirs in one pass**.

## New races (minted)

- **TSAN-0043 — `descr_get_qualname` lazy-cache write/write race (the notable new find, fileable).**
  `descr_get_qualname` (`descrobject.c:625`) does `if (descr->d_qualname == NULL) descr->d_qualname
  = calculate_qualname(descr);` with no critical section. Descriptors live on their (shared) type,
  so two threads first-reading `X.method.__qualname__` both write `d_qualname` → write/write race +
  leak. Same lazy-cache-without-lock class as `_elementtree` extra (TSAN-0041) and the objreduce
  cache (gh-125267, fixed). **`gh api` search found no existing issue → appears genuinely unfiled.**
  Reproduced in isolation (debug + release TSan). Report + repro packaged.

- **TSAN-0045 — `types.GenericAlias` iterator double-DECREF → SIGSEGV (fileable).** `ga_iternext`
  (`genericaliasobject.c:952`) is one-shot: `Py_SETREF(gi->obj, NULL)`. A shared `iter(list[int])`
  double-DECREFs `gi->obj` → refcount underflow / UAF. **Not just a TSan warning — it CRASHES:** the
  repro SIGSEGVs (exit 139) at `ga_iternext:952` **5/5 runs on both `debug-ft-nojit` AND
  `release-ft-nojit-o0`** (plain builds, no sanitizer), near-instantly. Crosses the gh-124397 "must
  not crash" bar unambiguously. Appears genuinely unfiled (distinct from the closed gh-153298
  `__parameters__` race). Low real-world likelihood (who shares a one-shot alias iterator?) but a
  hard crash from pure Python — **strong fileable candidate.** crash_backtrace.txt packaged.

- **TSAN-0044 — generic sequence iterator (`iter(obj)` seqiter) + `deque` iterator, value-benign.**
  `iter_iternext` writes `it->it_index++` (`iterobject.c:72`) vs `iter_len` reading it (`:100`);
  the `deque` iterator is the same class. This **is** gh-120496 ("Sequence iterator thread-safety",
  **CLOSED**) — its repro even tested "a custom class implementing `__getitem__`". It's value-benign
  (`PySequence_GetItem` is bounds-checked → duplicate/skip, not OOB), which per rhettinger's
  iterator strategy gh-124397 is **explicitly acceptable** ("concurrent access is allowed to return
  duplicate values, skip values, or raise"). **Not fileable** — cataloged for dedup, and notable
  purely as a demonstration that `--tsan-no-halt` unmasked a race every prior fleet hid.

## Folded faces (known races, new signature variants)

- `multibytecodec MultibyteStreamReader.reset` self-race (writes `self->state`/`pendingsize`
  unlocked) → **TSAN-0001** (the multibytecodec incremental-state class; StreamReader sibling of the
  IncrementalDecoder).
- count slow-mode faces `long_alloc | long_to_decimal_string_internal` (7 veh),
  `long_to_decimal_string_internal | x_add`, `count_repr | count_repr`, `? | count_nextlong`, and
  the cascade `SEGV Objects/object.c:PyObject_Repr` (6 veh) → **TSAN-0006** (count UAF residual of
  #153917; count_repr/count_nextlong on stack). The SEGV is the count UAF's downstream crash.
- `unpackiter_len | unpackiter_len` self-race → **TSAN-0039** (struct iter).
- `_lsprof Stop | Stop` → **TSAN-0008** (lsprof profiler state).
- `clear_extra | element_bool` → **TSAN-0041** (`_elementtree` extra; `element_bool` reads extra).

## Suppressed (noise)

- **tracemalloc**: `PyMem_GetAllocator | tracemalloc_*` and `PyMem_*Free | PyMem_SetAllocator` — the
  fuzzer exercises the `tracemalloc` module concurrently, but `tracemalloc.start()/stop()` swaps the
  process-global allocator (`PyMem_SetAllocator`), which inherently races every other thread's
  allocation. It's a debug tool, not an FT target — expected noise. Added to `suppressions.txt`.

## Left uncataloged (investigated — both known quantities, not folded on purpose)

Re-ingest drops the new-signature groups from **19 → 2**. Investigated both (per maintainer ask);
neither is a new bug, and neither is folded because both signatures are unsafe to catalog as-is:

- **`SEGV addr=0x? pc=0x5555556c4b6c`** — the **count-UAF cascade**. In its dir the race immediately
  before the SEGV is `_Py_TYPE_impl` (count_repr borrowing a freed long), i.e. **TSAN-0006**'s fatal
  downstream (the #153917 slow-mode residual). TSan couldn't unwind the nested SEGV, so the only
  signal is a raw `pc` — which is build-specific, so folding it into TSAN-0006 would go stale on the
  next rebuild. Left as a documented cascade artifact.
- **`_Py_atomic_load_ssize_relaxed | _Py_atomic_load_ssize_relaxed`** — actually **TSAN-0026** (dict
  iterator): the frames below the atomic accessor are `dictiter_iternext_threadsafe:6082` (read) vs
  `dictiter_iternextitem_lock_held:6017` (write). The signature collapsed to `atomic | atomic`
  because both stanzas' *innermost* resolved frame is `_Py_atomic_load_ssize_relaxed`. Folding the
  generic pair would mislabel any future atomic-ssize self-race. **The proper fix is a `tsan_dedup`
  parser improvement** — treat `_Py_atomic_*` accessor frames as plumbing (skip to the real caller),
  which would resolve this to `dictiter_iternext_threadsafe | dictiter_iternextitem_lock_held` (a
  clean TSAN-0026 face). That's a cross-repo-contract change (it would reshape existing
  `_Py_atomic_* | realfunc` catalog signatures), so it's deferred to a planned parser pass, not done
  inline here.

## Catalog

`known_races.tsv`: **145 → 159 signatures / 39 → 42 races** (3 mints + 5 folded variants + the
count/SEGV faces). Fleet-11 re-ingests to **2 new** (the leftovers above). Dedupe tally led by
TSAN-0006 (96), TSAN-0039 (47), TSAN-0026 (36), TSAN-0037 (33), TSAN-0038 (31), then TSAN-0040 (9),
TSAN-0041 (1) — our fleet-10 mints now deduping cleanly.

## Upstream posture

- **TSAN-0045 (genericalias iter)** — **CRASHES (SIGSEGV) on plain FT builds, genuinely unfiled →
  the strongest fileable finding of the fleet.** A hard crash from pure Python crosses the gh-124397
  bar regardless of the unusual sharing pattern. Awaiting go-ahead.
- **TSAN-0043 (descr qualname)** — genuinely new + unfiled lazy-cache write/write race; fileable
  (value-benign-ish leak, no crash observed). Awaiting go-ahead.
- **TSAN-0044 (seq/deque iter)** — gh-120496, closed as acceptable per gh-124397; nothing to file.
- The fleet-10 set/groupby/elementtree finds (TSAN-0040/0041/0042) were confirmed on their existing
  issues (#144356/#150791/#149816) in the previous session.
