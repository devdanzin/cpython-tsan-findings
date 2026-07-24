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
| **TSAN-0014** | concurrent `list.sort()` of a shared `list` — `list_sort_impl` detaches the array with **no** critical section and `binarysort` rewrites it in place, racing a concurrent iterator | low observed | take the list's per-object critical section in `list_sort_impl` | **reproduced 8/8** (`repro.py`, clean plain-`list` synthetic — barrier-isolated sort-vs-read; supersedes the shrinkray email-parser vehicle). Root cause grounded in current-main source. |

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

## Fleet 02 candidate round (TSAN-0022…0030)

Second deep-dive pass on the remaining new singles (all reproduced exit 66). Most were already
filed — the FT-hardening effort is fast. New/umbrella-worthy in **bold**.

| id | what | disposition |
|----|------|-------------|
| **TSAN-0026** | dict `dictiter_iternext_threadsafe:6043` plain-reads `ma_values` that `dictresize` publishes atomically (line `:6044` already reads it atomically) | **NEW, clean** — one-line incomplete-atomic-conversion; Yhg1s shared-container class |
| **TSAN-0030** | `sys.monitoring.use_tool_id` TOCTOU on interp-global `monitoring_tool_names[]` (leak + dup ownership; `free_tool_id` UAF) | **NEW, medium** — class of TSAN-0011 |
| **TSAN-0024** (epoll) | `select.epoll.close` is `@critical_section` but `fileno`/`register`/`poll` aren't (`epfd` plain int) | **NEW** — sibling of kqueue #151364; FileIO faces = #151707 |
| **TSAN-0029** | `trace_trampoline` writes `frame->f_trace` unlocked vs `@critical_section` accessors | **NEW, low-priority** (needs cross-thread frame mutation) |
| **TSAN-0025** | readline `set_auto_history` writes `should_auto_add_history` (static int) unlocked | NEW field, fold into the readline cleanup (#153291) |
| TSAN-0023 | weakref `subtype_getweakref` unlocked list-head read (UAF potential) | already reported **#149816** / PR #150247 |
| TSAN-0027 | `tp_subclasses` add-vs-clear asymmetric lock (UAF window) | already reported **#151377** |
| TSAN-0028 | RLock `repr` reads `lock.thread` plainly vs atomic writer | already reported+**fixed #153292** / PR #153299 |
| TSAN-0022 | elementtree `_setevents` | **folded** → TSAN-0013 (list faces) + TSAN-0009 (parser face) |

## Fleet 03 additions (TSAN-0031…0033)

`fusil-tsan_fleet_03` (6 inst, **917 crash dirs**, overnight 2026-07-16). The catalog deduped the
bulk (24 known races; 9 capped at the 30-vehicle sample cap). Of 42 first-pass "new" signature
groups: folded ~25 signature-variance faces into existing entries, suppressed 3 known FP/out-of-scope
families (tzset glibc, OpenSSL, subinterpreter) → **3 genuinely-new findings**. Final state: **0 new
signature groups, 0 framework**. Full triage in `notes/fleet-03-triage.md`.

This fleet also exposed **two dedup bugs in fusil's `tsan_dedup.py`**, both now fixed (branch
`tsan-dedup-atomic-stanza-parse`): (1) the `ACCESS` regex never matched TSan's lowercased
**`Previous atomic write`** header, so the 2nd stanza was dropped and the 1st duplicated into a
fabricated `A | A` signature — hitting the reader-vs-atomic-writer shape that is most of this catalog
(**18% of fleet-03's race dirs**), and mislabeling real RLock races as `tsanFRAME`; (2) mis-ordered
signatures in two metas silently never matched. Catalog signatures were **re-derived from evidence**
across all 1248 dirs of fleets 01–03. Two corrections fell out: TSAN-0014's `binarysort | binarysort`
was itself an artifact (true form `_Py_atomic_load_ptr | binarysort` confirms **sort-vs-read**), and a
**real bug was being suppressed** as "concurrent `bytearray.__init__`" when it is actually a
shared-list reader face of TSAN-0013.

| id | what | disposition |
|----|------|-------------|
| **TSAN-0033** | **`_asyncio.Task` refcount-0-while-GC-tracked premature free.** `TaskObj_dealloc` (`_asynciomodule.c:2963`) runs the finalizer + `unregister_task()` (which for a Task freed on a non-creator thread does a full `_PyEval_StopTheWorld`) **before** `PyObject_GC_UnTrack` (:2974); a concurrent `gc.collect()` stops the world in that window and `validate_refcounts` aborts on the refcount-0 tracked Task | **NEW, HIGH sev (memory-safety + liveness).** 58 vehicles. Reproduced 8/8 under TSan, 10/10 on plain debug-ft-nojit. **NOT Py_DEBUG-only — on `release-ft-nojit` it WEDGES THE INTERPRETER 5/5** (GC spins forever in `_mi_page_free_collect` ← `mi_heap_visit_blocks(update_refs)` ← `gc_collect_main`, 100% CPU, >150s; all other threads park on the runtime mutex), and a **segfault** was observed; identical-shape control completes in 0.11s (`release_hang_backtrace.txt`). Directly related to gh-142556 (its fix *created* this ordering) but distinct + unfiled. gh-116738 remit. Fix: untrack first in `TaskObj_dealloc`. NOT a data race — a crash/hang (no race signature; `crash_signature` in meta) |
| **TSAN-0031** | **`_elementtree.TreeBuilder` shared internal state.** every feed method (`start`/`data`/`end`/`comment`/`pi` → `treebuilder_handle_*` + `treebuilder_flush_data`/`extend_element_text_or_tail`) mutates the builder's `data`/`last`/`this`/`index`/`stack` fields with **no critical section** (module has zero); two threads feeding one shared builder race | **NEW, moderate** (benign-face reproduced, but unsynchronized refcount RMW on shared `PyObject*` fields + non-atomic `index++/--` → latent UAF/OOB). 18 vehicles, 8 signatures, reproduced 8/8. Closest prior art = **abandoned** PR gh-145569 (only `handle_end`). gh-116738 remit; distinct from TSAN-0009/0013/0022. Fix: `@critical_section` the whole feed path |
| **TSAN-0032** | `io.BufferedReader` iterator: `buffered_iternext`'s leading `CHECK_INITIALIZED` reads `self->ok` (`bufferedio.c:1504`) **before** it opens its own critical section (:1512), racing `_io._Buffered.detach`'s `self->ok = 0` (:628, which *is* `@critical_section`) | **RESIDUAL of a merged fix — already reported #149816 item #84** (PR **#150295** wrapped only `_buffered_readline`, left the leading check unprotected → incomplete). Reproduced 8/8. Latent NULL-deref (stale `ok==1` → detach nulls `raw` → reader derefs) — same shape as #153296 (StringIO). Recommend reopening #84 / follow-up PR |

## Fleet 04 additions (TSAN-0034)

`fusil-tsan_fleet_04` (6 inst, **155 crash dirs**, 2026-07-16). Short run, and the **first fleet with
the fixed `tsan_dedup` parser** (fusil #207) — in-loop labels are trustworthy for the first time
(~95 dirs self-labeled with a real `TSAN-*` id, **0 `tsanFRAME`**). The catalog held: 155 dirs → **20
known races deduped** + 24 suppressed + 26 noparse, only **2 new signature groups**, and after folding
**0 remain**. Full triage in `notes/fleet-04-triage.md`.

| id | what | disposition |
|----|------|-------------|
| **TSAN-0035** | shared `socket`: `sock_setblocking` plain-**writes** `s->sock_timeout` (`socketmodule.c:3172`) while `sock_gettimeout_impl` plain-**reads** it (`:3308`). All 15 accesses in the file are plain | **NEW, reproduced 10/10.** Incomplete FT conversion, proven: the sibling `sock_fd` on the **same struct** got atomic accessors (gh-128277/PR#128304) and `state->defaulttimeout` — same `PyTime_t` type — went atomic (gh-116616/PR#116623); `:1134` atomically loads the default and **plain-stores** it into `sock_timeout`. **gh-128277 "Make socket module thread safe" is CLOSED but never touched `sock_timeout`**, and **gh-116738 already ticks `- [x] socketmodule.c`** — premature. `_ssl.c` inherits it (4 plain reads). LOW severity, honestly: two impact stories were **refuted** (compiler coalesces the double-read; 0/28,800 racing pairs diverged). Fix: `get/set_sock_timeout` mirroring the `sock_fd` pair |
| **TSAN-0036** | instrumentation: `no_tools_for_local_event` (via `_PyEval_NoToolsForUnwind`, `ceval.c:2465`, from `gen_close`) plain-reads 1 byte of `code->_co_monitoring->active_monitors.tools[]` with no lock and no version check, while `force_instrument_lock_held` (`instrumentation.c:1842`) replaces the struct under `LOCK_CODE` only | **NEW, reproduced 6/6.** `LOCK_CODE` can't help — the eval loop never takes it; STW registration only re-instruments *executing* code, the rest is re-instrumented **lazily** from the `RESUME` version check with the world running (that's the racing writer). Provable incomplete conversion: 13 `FT_ATOMIC_*` uses in the file cover exactly the lock-free-read state, but `active_monitors.tools[]` has none; line 1842 unchanged since PEP 669 (2023). **Strong prior art: gh-136870 / PR #136994** converted four `LOCK_CODE` sites to STW *precisely because `LOCK_CODE` doesn't exclude eval-loop readers* — but only for bytecode tool bytes; **this is the sibling that fix missed**. Distinct from TSAN-0030 (tool-id registry) and TSAN-0029 (frame `f_trace`). LOW severity (missed/spurious event). Fix: relaxed atomics on the bytes |
| **TSAN-0034** | interpreter finalization: `handle_thread_shutdown_exception` does `assert(interp->threads.head != NULL)` (`pylifecycle.c:3830`) — an **unlocked** read — on the line *before* `_PyEval_StopTheWorld`, racing a concurrent `HEAD_LOCK`-held write of `interp->threads.head` (`add_threadstate` on thread create / `tstate_delete_common` on thread exit) | **NEW but LOW / debug-build only.** The racing read exists only inside the `assert` (`NDEBUG` removes it), so release builds cannot hit it. The function's comment ("we don't have to worry about locking this because the world is stopped") doesn't cover this line. **Reproduced in isolation** (`repro.py`, ~44 %/run — continuous `_thread` churn through finalization; the earlier 0/14 created threads once at startup). No filing exists (verified via `gh api`). Fix: move the assert below the STW |
| TSAN-0024 (epoll `poll` face) | `pyepoll_internal_close \| select_epoll_poll_impl` | **folded** → TSAN-0024, which already held the `fileno` face and predicted `poll` was unguarded |
| TSAN-0033 | 8 more `validate_refcounts` / `_asyncio.Task` vehicles | independent confirmation on a fresh fleet |
| TSAN-0031 | 1 vehicle | independent confirmation of the fleet-03 TreeBuilder finding |

## Fleet 10 additions (TSAN-0040…0042)

`fusil-tsan_fleet_10` (4 inst, **170 crash dirs**, 2026-07-18). First fleet on the matrix rebuilt
onto main `a1d580430c8` (with the count fast-mode fix #153917); still `--tsan-weird-subclasses`,
still `halt_on_error=1`. Removing the count fast-mode shadow surfaced the cross-session diversity:
**9 new signature groups → 3 new races (reproduced in isolation, exit 66) + 5 folded faces**.
`known_races.tsv` 132 → 145 sigs / 36 → 39 races; re-ingests 0 new. Full triage in
`notes/fleet-10-triage.md`.

| id | what races | disposition |
|----|-----------|-------------|
| **TSAN-0041** | `_elementtree`: `element_attrib_getter` (`.attrib`) etc. do an unsynchronized `if (!self->extra) create_extra(...)`, and `create_extra` (`_elementtree.c:274`) writes `self->extra = PyMem_Malloc(...)` with no critical section → concurrent first-touch of a shared Element write/write-races `self->extra` (+ leak), and racing readers (`element_length`) / `clear_extra` | **ALREADY REPORTED — gh-149816; fix pending in OPEN PR #149918** (wraps `create_extra`/`element_attrib_getter`/`element_get_attrib`/`element_length`/`clear_extra`/traverse in `Py_BEGIN_CRITICAL_SECTION(self)`). Independently reproduced by fusil (3 veh) on debug **and** release TSan builds. **Not a new filing** — confirm on #149816/#149918, move to `fixed` when merged. `status: reported` |
| **TSAN-0040** | `set` iterator: `setiter_len` (`setobject.c:1063`, via `operator.length_hint`) reads the shared iterator's `si->len`/`si_used` while `setiter_iternext` advances them (`si->len--`, `si->si_pos`) | **ALREADY REPORTED — gh-144356; fix pending in OPEN PR #144357** (runs `setiter_len` under `Py_BEGIN_CRITICAL_SECTION(op)` and `setiter_iternext` under `Py_BEGIN_CRITICAL_SECTION2(self, so)` with the `si->si_pos`/`si->len--` writes inside — covers this exactly). Value-benign per the iterator strategy gh-124397 (fetch is under the set CS). Reproduced on debug+release. **Not a new filing** — confirm on #144356, → `fixed` when #144357 merges. Background: gh-112069/#117935 fixed the set-access half |
| **TSAN-0042** | `itertools.groupby`: a shared groupby's `groupby_next` (`itertoolsmodule.c:537`) mutates `gbo->currkey/currvalue/tgtkey/currgrouper` with **no** critical section (faces `groupby_next\|groupby_next`, `_grouper_create\|groupby_next`) | **ALREADY REPORTED — gh-150791; fix pending in OPEN PR #150792** ("add critical section for `groupby.next`"). Crosses the gh-124397 "don't crash" bar (corrupts state → `AttributeError` on live objects). The merged gh-143543/#146613 are re-entrancy-only, orthogonal; gh-123471 doesn't list groupby, but #150791 tracks it directly. Reproduced on debug+release. **Not a new filing** — confirm on #150791, → `fixed` when #150792 merges |
| folds | `dictiter…\|dictiter_iternextkey` → **TSAN-0026**; `unicodeiter_next\|unicodeiter_next` + `unicodeiter_len\|unicodeiter_next` → **TSAN-0038** (general non-ASCII unicode iterator; same #153928); `_decimal_Context_clear_traps_impl\|type_call` → **TSAN-0019**; `_PyLong_DigitCount\|_PyMem_DebugRawAlloc` → **TSAN-0006** (count slow-mode UAF residual of #153917, count_repr on stack) | 5 new faces of known races, folded |

## Fleet 11 additions (TSAN-0043…0045) — first `--tsan-no-halt` fleet

`fusil-tsan_fleet_11` (4 inst, **223 crash dirs**, 2026-07-18). **First fleet with `--tsan-no-halt`**
(multiple races per session, fusil #221/#222). **82/223 dirs carried a `tsan_races.tsv` sidecar.**
Multi-race captured **344 race instances / 62 distinct signatures** vs the **41** a `halt_on_error=1`
fleet would have seen — **129 instances masked before, 21 signatures never a first race**. Full
triage + stats in `notes/fleet-11-triage.md`.

| id | what races | disposition |
|----|-----------|-------------|
| **TSAN-0043** | descriptor `__qualname__`: `descr_get_qualname` (`descrobject.c:625`) does `if (!descr->d_qualname) descr->d_qualname = calculate_qualname(...)` with no lock → concurrent first-read of a shared descriptor's `__qualname__` write/write-races `d_qualname` (+ leak) | **NEW, reproduced, appears UNFILED** (gh api search empty). Lazy-cache class (cf. `_elementtree` TSAN-0041, objreduce gh-125267). Descriptors live on shared types → realistic. **Strongest fleet-11 fileable candidate**, awaiting go-ahead |
| **TSAN-0045** | `types.GenericAlias` iterator: `ga_iternext` (`genericaliasobject.c:952`) `Py_SETREF(gi->obj, NULL)` on a shared one-shot `iter(list[int])` → double-DECREF / UAF | **FILED → cpython#154043.** CRASHES (SIGSEGV) at `ga_iternext:952` 5/5 runs on plain `debug-ft-nojit` AND `release-ft-nojit-o0` (no sanitizer), near-instant. Crosses the gh-124397 "must not crash" bar. Distinct from *closed* gh-153298 (`__parameters__`). crash_backtrace.txt packaged |
| **TSAN-0044** | generic sequence iterator (`iter(obj)` seqiter, `iterobject.c:72/100`) + `deque` iterator: non-atomic `it_index`/cursor | **= gh-120496 (CLOSED), value-benign.** `PySequence_GetItem` is bounds-checked → duplicate/skip, not OOB; acceptable per rhettinger's iterator strategy gh-124397. **Not fileable** — cataloged for dedup. Notable as proof `--tsan-no-halt` unmasks races halt=1 hid |
| folds | `multibytecodec StreamReader.reset` → **TSAN-0001**; count faces `long_alloc\|long_to_decimal` + `count_repr\|count_repr` + cascade `SEGV PyObject_Repr` → **TSAN-0006**; `unpackiter_len\|unpackiter_len` → **TSAN-0039**; `_lsprof Stop\|Stop` → **TSAN-0008**; `clear_extra\|element_bool` → **TSAN-0041**. tracemalloc allocator-swap races → **suppressed** | 5 folded + noise suppressed |

## Fleet 12 additions (TSAN-0046…0048) — second `--tsan-no-halt` fleet

`fusil-tsan_fleet_12` (4 inst, **270 crash dirs**, 2026-07-19, 107 sidecars). **0 new fileable races
— coverage converging.** 392 race instances / 70 distinct captured vs 51 first-only (122 masked, 19
only-via-multi). Full triage in `notes/fleet-12-triage.md`.

| id | what races | disposition |
|----|-----------|-------------|
| **TSAN-0046** | `io.IncrementalNewlineDecoder`: `.reset()` writes `self->seennl` (`textio.c:630`) unlocked vs `.newlines` reading it | **= cpython#144777 (CLOSED).** Value-benign; reproduced. Cataloged for dedup |
| **TSAN-0047** | `locale.localeconv()`: concurrent calls race the non-thread-safe C `localeconv()` static `struct lconv` → **heap-use-after-free** of its strdup'd fields | **= cpython#127081 (OPEN, "Thread-unsafe libc functions").** Memory-unsafe but libc-rooted; fix is CPython-side locking. Cataloged for dedup |
| **TSAN-0048** | `csv.reader`: `Reader_iternext` writes `self->line_num` (`_csv.c`) vs a concurrent `reader.line_num` member read | **NEW but value-benign** (stale counter, no crash). Reproduced; appears unfiled. Low priority — not proposing a filing |
| folds/flags | `_PyLong_DigitCount\|_PyMem_DebugRawFree` → **TSAN-0006**. Left uncataloged: count-cascade SEGV (pc…4b6c) + dict-iter `atomic\|atomic` artifact (both fleet-11 knowns); an unsymbolized `_lsprof` SEGV; and **`sock_accept_impl\|sock_finalize`** — a TSan **fd-close-while-in-use** race (fd closed by socket dealloc vs a concurrent `accept4`). **INVESTIGATED: non-reproducible** (0/24 vehicle replays, 3 synthetic repros failed) → shrinkray N/A; likely a fuzzer timing artifact, not fileable | 1 fold + 4 documented leftovers |

## Fleet 15 additions (TSAN-0053, TSAN-0054) — first **un-masking** fleet (gateway suppressions)

`fusil-tsan_fleet_15` runs the **un-masking profile** (`--tsan-no-halt` +
`--tsan-suppressions=gateway_suppressions.txt`, which TSan-suppresses the gateway iterator/`count`
races that otherwise dominate). The very first fileable hit is a **crash the gateway was hiding** —
exactly the design intent. It lands as `…-tsanNOPARSE` because it is an abort/segfault, **not** a TSan
data-race report.

| id | what | disposition |
|----|------|-------------|
| **TSAN-0053** | plain `dict` iterator: `dictiter_iternext_threadsafe`'s exhaustion path `fail: di->di_dict = NULL; Py_DECREF(d);` (`dictobject.c:6158-6159`) non-atomically drops the iterator's **one** owning ref to the dict, while the caller `dictiter_iternextkey:5791` reads `d = di->di_dict` unlocked → two `next()` threads both `Py_DECREF(d)` → **double-free / UAF / negative refcount** | **FILED → cpython#154130.** NEW crash, reproduced ~8/8 SIGABRT on `debug-ft-nojit`, SIGSEGV on `release-ft-nojit-o0`. Crash face of *closed* cpython#148873 (dup of value-benign gh-120496). **NEW FACE from fleet-15: long-lived shared `frozendict`** (`iter(frozendict)` → `dict_keyiterator`; module-constant refcount>1) → the underflow is silent until `gc.collect` catches it (`gc_free_threading.c` "refcount too small") — the *more dangerous* face (9 veh across symtable/curses/gettext/functools/json/pickletools/opcode). `repro_frozendict_gc.py` added |
| **TSAN-0054** | **set/frozenset iterator**: `setiter_iternext`'s exhaustion path `si->si_set = NULL; Py_DECREF(so);` (`setobject.c:1130-1131`, **outside** the `so` critical section) with an unlocked read of `so = si->si_set` (`:1101`) → two `next()` threads both `Py_DECREF(so)` → **double-free / UAF** | **NEW crash, reproduced (corroborate, don't file).** ~8/8 `_Py_NegativeRefcount` @ `setobject.c:1131` on `debug-ft-nojit`; 6/6 SEGV core-dumped on `release-ft-nojit-o0`; long-lived `frozenset` face = GC "refcount too small". **Set sibling of TSAN-0053**; distinct from the value-benign set-iter cursor race **TSAN-0040** (cpython#144356). Its open fix PR **cpython#144357** (`CRITICAL_SECTION2(self, so)` + drop the exhaustion DECREF under FT) **fixes it but is stalled since 2026-05** → corroborate #144356/#144357 with the crash repro to unstall |
| faces/known | **`_asyncio.Task` ×10** (`gc_free_threading.c:1083` validate_refcounts) = **TSAN-0033** (#153809). **dict `:6159` ×7** + `_colorize` critical-section-assert ×1 = **TSAN-0053**. **str `it_seq` double-DECREF** (`unicodeobject.c:14964`, 1 veh) = the **str face** of the same class (rarer in isolation — strings often interned/immortal). All part of the **systemic sequence-iterator exhaustion double-DECREF class** (`notes/sequence-iterator-exhaustion-double-decref.md`): every `*_iternext` exhaustion does `it_seq/di_dict/si_set = NULL; Py_DECREF(seq)` non-atomically (dict/set confirmed crashers; str/bytes/tuple/list/seqiter same shape) | known re-finds + 1 systemic-class note |

## Fleet addition (TSAN-0055) — free-threaded cryptography `--tsan` fleet (`fusil-cryptography_02`)

| id | race | disposition |
|----|------|-------------|
| **TSAN-0055** | **`memoryview` iterator**: `memoryiter_next`'s exhaustion path `it->it_seq = NULL; Py_DECREF(seq);` (`memoryobject.c:3641-3642`) with an unguarded read `seq = it->it_seq` (`:3623`) and **no critical section anywhere in the function** → two `next()` threads both `Py_DECREF(seq)` → **double-free / UAF**. Same function also carries the value-benign `it_index` cursor race (`:3628`/`:3633`); both collapse to one signature `memoryiter_next \| memoryiter_next` | **NEW crash, reproduced (corroborate, don't file).** `repro.py` aborts **4/4** `_Py_NegativeRefcount` @ `memoryobject.c:3642` on `debug-ft-nojit`; **SEGV in `memoryiter_next` on `release-ft-nojit-asan`** (genuine UAF, not debug-only). **memoryview sibling of TSAN-0053 (dict) / TSAN-0054 (set)** — the "same shape (not yet tripped)" row of the exhaustion-double-DECREF class, now tripped; `memoryiter_next` is worse (NO critical section at all). Incidental to cryptography (pure-CPython frames). Corroborate the class on cpython#124397 / umbrella #153852; value-benign cursor face = gh-120496/#124397 |

**Why the un-masking fleet found them:** the value-benign dict/set-iterator data races are suppressed
gateways (`race:dictiter_iternext` / `race:setiter_iternext`). With those suppressed, the rarer
double-DECREF **crashes** surfaced — concrete proof the un-masking profile exposes the tail crashers the
standard fleet's first-race gateways bury. The **long-lived-object faces** (frozendict/frozenset caught by
GC) are the realistic, dangerous manifestation: a shared module-level frozen mapping/set iterated from
threads silently corrupts memory. See `notes/sequence-iterator-exhaustion-double-decref.md`.

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

## magalu_tsan additions (TSAN-0056, TSAN-0057) — cloud `--tsan` fleet, 2 single sessions

| id | race | disposition |
|----|------|-------------|
| **TSAN-0056** | **`contextvars.Context` (HAMT) iterator**: the whole traversal cursor (`i_nodes[]` borrowed, `i_pos[]`, `i_level`) lives in the iterator and is advanced with plain reads/writes and **no critical section** (`hamt_iterator_next` `hamt.c:2185`, `hamt_iterator_bitmap_next` `:2083`). Two threads advancing one shared `iter(ctx)` desync `i_level` vs `i_nodes[]` → `current = i_nodes[i_level]` is stale/NULL/wild → `IS_BITMAP_NODE(current)` = `Py_TYPE(current)` → **SIGSEGV** | **NEW crash, reproduced. FILE (strong).** `repro.py` **6/6 SIGSEGV** on `debug-ft-nojit`; SEGV in `_Py_TYPE_impl` ← `hamt_iterator_bitmap_next` ← `hamt_iterator_next` on `debug-ft-nojit-asan`. **Crosses the gh-124397 "C iterators must not crash" bar** — HAMT/Context iterator never hardened. Sibling of TSAN-0053 (dict/#154130), TSAN-0054 (set/#144357), TSAN-0055 (memoryview). No upstream hamt-iter issue (open #148891/#150178 are GC/alloc, unrelated) |
| **TSAN-0057** | **shared `_pickle.Pickler`**: no per-object critical section, so concurrent `dump()` / `clear_memo()` on one Pickler race its `PyMemoTable` — `PyMemoTable_Clear` `Py_XDECREF(me_key)` + `memset` (`_pickle.c:816-819`) vs `_PyMemoTable_Lookup`/`PyMemoTable_Size` reads (`:807`/`:848`) and `memo_put` resize — plus the shared output buffer (`_Pickler_Write` `:1106`) | **NEW crash, reproduced.** `repro.py` **6/6** on `debug-ft-nojit` (`_Py_NegativeRefcount` SIGABRT + `_Pickler_Write` SIGSEGV). "Shared mutable object w/o locking" class (cf. multidict). **Distinct from the Unpickler-memo bug #150505/PR#150550.** **DO NOT FILE** — on #150505 serhiy-storchaka + kumaraditya303 ruled sharing pickler/unpickler objects across threads out of scope ("I do not see what can be a reason of sharing pickler or unpickler objects between threads"); kept as documentation only. (Distinct from #149816 items 89/91 = UAFs from pickling *shared data*, a legitimate scenario.) |

**magalu_tsan follow-on (2 more sessions, 2026-07-23) — both KNOWN/out-of-scope, no new report:**
- `email_iterators` → **TSAN-0007** (StringIO `self->pos`/buffer unlocked, cpython#153296) — new *readline-vs-readline* face (`_stringio_readline | _stringio_readline`) folded into TSAN-0007's signatures.
- `concurrent_futures_interpreter` → **subinterpreter machinery, out of scope per cpython#143232** (concurrent `Py_NewInterpreterFromConfig` via `InterpreterPoolExecutor` → `init_static_exctypes`/`type_ready_set_base`/`_PyExc_InitTypes` racing static exc-type init). 4 pairings already suppressed; the 2 `crossinterp_exceptions.h:init_static_exctypes` pairings added to `suppressions.txt`.
- TSAN-0056 (contextvars/HAMT) **FILED → cpython#154535**.

## magalu_tsan batch 3 (2026-07-24, 8 sessions) — 1 new crasher (TSAN-0058), rest known/benign/borderline

| session | verdict |
|---------|---------|
| `_elementtree` + `xml_etree_cElementTree` | **KNOWN → TSAN-0058 = cpython#146022.** Shared `Element` mutate/read (`clear_extra`/`create_extra`/`dealloc_extra`/`element_add_subelement`/`_set_joined_ptr` vs `element_length`/`element_get_tail`) — module is `Py_MOD_GIL_NOT_USED` with no critical sections → child-refcount corruption → **`element_dealloc` `Py_REFCNT==0` abort, 5/5**. Element-side companion of TSAN-0031 (TreeBuilder). **Prior art: cpython#146022** ('Make Element usable on free-threaded builds' — describes this exact clear-vs-read crash) + #149816 items 69/87 (fix PR #149918). NOT a new filing; corroborates #146022. |
| `string_templatelib` | **KNOWN → TSAN-0052** (t-string `templateiter_next` `from_strings`-flag race). Value-benign (0/5 crash, gh-124397 "may dup/skip" is allowed). Added the `templateiter_next\|builtin_next` top-frame variant. |
| `shlex` | **KNOWN → TSAN-0007** (StringIO `self->pos`/buffer unlocked, cpython#153296). |
| `_android_support` | **KNOWN → cpython#154523** (`TextIOWrapper.detach()` buffer slot). Value-benign on aligned hw (verified earlier); already filed. Suppressed. |
| `socket` (`sock_initobj_impl\|socket_close`) + `mailbox` (`_io_FileIO___init___impl\|internal_close`) | **Borderline / not minted.** Concurrent re-`__init__` of a shared socket/FileIO racing `close` — fd-lifecycle races (FileIO side is the TSAN-0024 / cpython#151707 family; socket is the analogue). Re-initialising a live shared object is unusual; left documented, uncataloged. |
| `_colorize` | **Low-confidence / not minted.** Primary = `new_dict_impl \| _Py_atomic_load_ssize_relaxed` (dict-internal) with a secondary `SEGV _PyObject_GenericGetAttrWithDict` (only-via-multi-race) from concurrent attr churn on a weird-subclass instance; did NOT reproduce from plain shared-object attr churn (0/5). Needs the vehicle to pin. |
