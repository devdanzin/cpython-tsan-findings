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
