# TSAN findings index

Status board for the ThreadSanitizer data races found by `fusil --tsan` in free-threaded CPython.
Entries were **root-caused and reproduced in isolation** (minimal stdlib-only repro, confirmed exit
66 on `debug-ft-nojit-tsan`, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, glibc 2.43)
unless a row says otherwise. Found in `fusil-tsan_fleet_01` (2026-07-15).

> **Ruling 2026-07-15 (Thomas Wouters / Yhg1s, CPython RM):** the "concurrent unsynchronized access
> to a shared builtin" class **is a bug** (our `shared_list_race.py` + note were sent verbatim; reply
> "Yes, that's a bug"). So the shared-list races we had suppressed/held are now valid findings —
> **TSAN-0013** (non-atomic list readers), **TSAN-0014** (concurrent `list.sort()`), **TSAN-0010**
> (marshal reader). The glibc/TSan false positives (tzset, TSAN-0003) are unaffected and stay
> suppressed.

## Real, new CPython free-threading bugs (report-worthy)

| id | what races | severity | fix | notes |
|----|-----------|----------|-----|-------|
| **TSAN-0001** (+**0004**) | cjkcodecs `MultibyteIncrementalDecoder`: `getstate()`/`reset()`/`decode()` on the unsynchronized `pending`/`pendingsize`/`state` fields | low–med (value-benign face; buffer/len tear latent) | per-object critical sections on the incremental codec methods (`multibytecodec.c` has none) | **0004 is the same bug** (`state.c` face vs 0001's `pendingsize` face). Distinct from #152767/PR#153000, which lock **only** the `errors` setter. |
| **TSAN-0002** | `_zstd.ZstdCompressor`: plain store of `last_mode` (`compressor.c:679`) vs the `Py_T_INT` member descriptor's relaxed-atomic read | low (value-benign) | `FT_ATOMIC_STORE_INT_RELAXED` on the 4 `last_mode` stores | incomplete atomic conversion; member-read atomics landed, `_zstd`'s stores (new in 3.14) were missed. |
| **TSAN-0005** | `decimal.Decimal.__hash__`: lazy hash cache `self->hash` written without atomics (`_decimal.c:5924/5925`) | low (value-benign) | relaxed atomics on `self->hash` | `hash()` looks read-only; distinct from the decimal `mpd_context_t.status` race (#149142). |
| **TSAN-0006** | `itertools.count`: `count_repr` plain-reads `cnt` (`:3612`) while `count_next` writes it with an **atomic CAS** (`:3599`) | low (value-benign) | `_Py_atomic_load_ssize_relaxed` in `count_repr` | incomplete atomic conversion (writer hardened, reader missed). `count` not covered by #151409/#144357/#153062. |
| **TSAN-0011** | `sys.addaudithook`: unlocked lazy init of `interp->audit_hooks` (`sysmodule.c:540`) vs `should_audit` (`:239`) | low–mod (rare fail-open dropped hook) | serialize under the existing `runtime->audit_hooks.mutex`; atomics on the pointer | **security-relevant** (PEP 578). C-level hook list is already mutex-guarded — incomplete FT migration (git-blame-proven). Distinct from #152912/#152913 (exception handling). |
| **TSAN-0013** | shared `list`: non-atomic readers (`Py_SIZE`/unpack, `stringlib_bytes_join`, `w_complex_object`) race `list_resize`'s atomic `ob_item`/`ob_size` publish | low (value-benign) but **systemic** | convert list read sites to the atomic accessors the writer uses (`_PyList_GetItemRef`, atomic `Py_SIZE`) | the class Yhg1s ruled a bug; two faces reproduced (`repro_size.py`/`repro_join.py`). Squarely in gh-116738's remit. |
| **TSAN-0010** | `marshal.dumps(shared_list)` reads `ob_item[]` (plain `PyList_GET_ITEM` in `w_complex_object`) while another thread `append`s | low (value-benign); `list_resize` variant is a latent UAF | `Py_BEGIN_CRITICAL_SECTION(v)` around the list/dict walk (the set-branch of the same function already does) | a reader face of TSAN-0013; the set-branch asymmetry shows it's a missed conversion. |
| **TSAN-0014** | concurrent `list.sort()` of a shared `list` — `list_sort_impl` detaches the array with **no** critical section and `binarysort` rewrites it in place, racing a concurrent iterator | low observed | take the list's per-object critical section in `list_sort_impl` | **reproduced** (`repro.py`, shrinkray-minimized 994→28 lines; ~15–30 %/run — it's sort-vs-*read*, not sort-vs-sort). Root cause grounded in current-main source. |

## Already reported upstream

| id | what | upstream |
|----|------|----------|
| **TSAN-0007** | `io.StringIO`: `tp_iternext` slot bypasses the per-object critical section that every clinic method takes, racing `seek()`/`readline()` on `self->pos` (buffer-realloc UAF latent) | **python/cpython#153296** (fix in PR #153368 — wraps `stringio_iternext` in the critical section, exactly the needed fix) |

## Not a CPython bug

| id | what | disposition |
|----|------|-------------|
| **TSAN-0003** | `_multiprocessing.SemLock` create/destroy → glibc `tsearch`/`tdelete` on the process-global `__sem_mappings` tree | **glibc/TSan false positive** — glibc serializes with its internal `__sem_mappings_lock` (an lll lock TSan can't see); confirmed by glibc-2.43 disassembly. Same class as the tzset false positive. **Suppressed** in `catalog/suppressions.txt`. |
| **TSAN-0009** | pyexpat parser: `SetReparseDeferralEnabled()` writes `m_reparseDeferralEnabled` vs `callProcessor()` read | **expected** — bundled single-threaded libexpat; a parser is not thread-shareable by design. Catalog data point, not for individual filing. |

## Known-area residual (low priority, not a headline bug)

| id | what | disposition |
|----|------|-------------|
| **TSAN-0008** | `_lsprof`/`cProfile`: `profiler_dealloc` teardown (`flush_unmatched`/`clearEntries`) has no critical section, races/UAFs `currentProfilerContext` vs an in-flight monitoring callback during concurrent *drop* | Reproduces on **current main**, but the cProfile FT-safety class was already addressed by **gh-116738 / PR #138229** (critical sections on the *profiling* path). This is the residual **teardown** edge that fix didn't cover — `tp_dealloc` can't easily take a critical section, plus a borrowed-ref window in the `sys.monitoring` dispatch. History: #125165 (+ #126884) were closed **NOT_PLANNED** (colesbury: `_lsprof` not thread-safe w/o GIL, per-profiler locks "won't be efficient"). **Low priority; at most a note on #138229/gh-116738 about the teardown gap — not a standalone filing.** Distinct from the re-entrant-timer UAF #143545. |

## Related non-race catalog entries

| id | what | upstream |
|----|------|----------|
| TSAN-0012 | faulthandler `is_enabled()` reads `fatal_error.enabled` that `enable()`/`disable()` write | already reported **#151363** |

## Open dev-questions (not individual races)

See `notes/open-questions-for-umbrella.md`. Q1 (concurrent `list.sort()`) is now **answered** by the
Yhg1s ruling → promoted to **TSAN-0014**. The remaining note there is the tzset/mktime glibc/TSan
false positive (resolved, not a bug).

## Fleet 02 additions (TSAN-0015…0021)

`fusil-tsan_fleet_02` (290 dirs; catalog deduped the bulk — TSAN-0013 alone caught 50). Of 45 new
signature groups, 7 were deep-dived (all reproduced exit 66); the rest are dispositioned in
`notes/fleet-02-triage.md` (known families, subinterpreter/init out-of-scope, folds).

| id | what | disposition |
|----|------|-------------|
| **TSAN-0018** (+**0021**) | **dict split-keys**: `object.__getstate__` and `dict.clear` read a type's shared `dk_nentries` with a plain load while `setattr` bumps it atomically (`split_keys_entry_added`) | **REAL, NEW, filable** — one-line-per-site fix (`LOAD_KEYS_NENTRIES`, which already exists at `dictobject.c:237`). 0021 = the `clear_lock_held` reader face, folded in. |
| TSAN-0015 | OrderedDict `odictiter_new` (unlocked) vs `clear()` → UAF | already reported **#151627** (PR #151688) |
| TSAN-0016 | `readline.get_completer()` reads `readlinestate.completer` unlocked vs the locked setter | already reported **#153291** (PR #153362) |
| TSAN-0019 | `decimal.Context` `clear_flags` vs `repr` on `mpd_context_t.status` | dup of **#149142** (PR #150598) |
| TSAN-0017 | `_zstd` `flush\|flush` | dup of **TSAN-0002** (same `last_mode`; folded) |
| TSAN-0020 | `tp_new_wrapper\|tp_new_wrapper` | **out of scope** — OpenSSL-internal libcrypto race (each thread builds its own `SSLContext`); like TSAN-0003 |

## Cross-check

None of these overlap **#149816** ("22 free-threading race conditions") — that umbrella covers
entirely different modules (`_random`/`_ssl`/`typeobject`/`listobject`/`_pickle`/`dict`/`bytes`/
`memoryview`/`_struct`/`_ctypes`/`_elementtree`/`bufferedio`).

The report-worthy findings were also cross-checked against **gh-116738** ("Audit all built-in
modules for thread safety") and confirmed still-unfixed on **current main** (`heads/main:bcf98ddbc40`):
`multibytecodec.c` (0001/0004), `_decimal.c` (0005), `itertoolsmodule.c` (0006), `sysmodule.c` (0011)
and `listobject.c`/list readers (0013/0014/0010) are all **unchecked** on that audit list, and
`_zstd/` (0002) isn't listed at all (newer 3.14 module) — no merged audit PR touches any of them. The
shared-list class (0013/0014/0010) is squarely gh-116738's remit (builtin-container thread-safety).
By contrast `_lsprof.c` is **checked** on that list, which is why **TSAN-0008 is a residual of
completed work**, not a new finding.
