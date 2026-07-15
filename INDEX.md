# TSAN findings index

Status board for the ThreadSanitizer data races found by `fusil --tsan` in free-threaded CPython.
All entries below were **root-caused and reproduced in isolation** (minimal stdlib-only `repro.py`,
confirmed exit 66 on `debug-ft-nojit-tsan`, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`,
glibc 2.43) unless noted. Found in `fusil-tsan_fleet_01` (2026-07-15).

## Real, new CPython free-threading bugs (report-worthy)

| id | what races | severity | fix | notes |
|----|-----------|----------|-----|-------|
| **TSAN-0001** (+**0004**) | cjkcodecs `MultibyteIncrementalDecoder`: `getstate()`/`reset()`/`decode()` on the unsynchronized `pending`/`pendingsize`/`state` fields | low–med (value-benign face; buffer/len tear latent) | per-object critical sections on the incremental codec methods (`multibytecodec.c` has none) | **0004 is the same bug** (`state.c` face vs 0001's `pendingsize` face). Distinct from #152767/PR#153000, which lock **only** the `errors` setter. |
| **TSAN-0002** | `_zstd.ZstdCompressor`: plain store of `last_mode` (`compressor.c:679`) vs the `Py_T_INT` member descriptor's relaxed-atomic read | low (value-benign) | `FT_ATOMIC_STORE_INT_RELAXED` on the 4 `last_mode` stores | incomplete atomic conversion; member-read atomics landed, `_zstd`'s stores (new in 3.14) were missed. |
| **TSAN-0005** | `decimal.Decimal.__hash__`: lazy hash cache `self->hash` written without atomics (`_decimal.c:5924/5925`) | low (value-benign) | relaxed atomics on `self->hash` | `hash()` looks read-only; distinct from the decimal `mpd_context_t.status` race (#149142). |
| **TSAN-0006** | `itertools.count`: `count_repr` plain-reads `cnt` (`:3612`) while `count_next` writes it with an **atomic CAS** (`:3599`) | low (value-benign) | `_Py_atomic_load_ssize_relaxed` in `count_repr` | incomplete atomic conversion (writer hardened, reader missed). `count` not covered by #151409/#144357/#153062. |
| **TSAN-0008** | `_lsprof`/`cProfile`: `profiler_dealloc` teardown (`flush_unmatched`) not critical-section-guarded, races/UAFs `currentProfilerContext` vs an in-flight monitoring callback | **med–high (UAF/SEGV)** | unregister monitoring events before freeing; run teardown under the object critical section | monitoring rewrite makes one `enable()` interpreter-wide, so exposure isn't opt-in. Distinct from the re-entrant-timer UAF #143545. |
| **TSAN-0011** | `sys.addaudithook`: unlocked lazy init of `interp->audit_hooks` (`sysmodule.c:540`) vs `should_audit` (`:239`) | low–mod (can silently drop a hook) | serialize under the existing `runtime->audit_hooks.mutex`; atomics on the pointer | **security-relevant** (PEP 578). C-level hook list is already mutex-guarded — incomplete FT migration. Distinct from #152912/#152913 (exception handling). |

## Already reported upstream

| id | what | upstream |
|----|------|----------|
| **TSAN-0007** | `io.StringIO`: `tp_iternext` slot bypasses the per-object critical section that every clinic method takes, racing `seek()`/`readline()` on `self->pos` (buffer-realloc UAF latent) | **python/cpython#153296** (fix in PR #153368 — wraps `stringio_iternext` in the critical section, exactly the needed fix) |

## Not a CPython bug

| id | what | disposition |
|----|------|-------------|
| **TSAN-0003** | `_multiprocessing.SemLock` create/destroy → glibc `tsearch`/`tdelete` on the process-global `__sem_mappings` tree | **glibc/TSan false positive** — glibc serializes with its internal `__sem_mappings_lock` (an lll lock TSan can't see); confirmed by glibc-2.43 disassembly. Same class as the tzset false positive. **Suppressed** in `catalog/suppressions.txt`. |
| **TSAN-0009** | pyexpat parser: `SetReparseDeferralEnabled()` writes `m_reparseDeferralEnabled` vs `callProcessor()` read | **expected** — bundled single-threaded libexpat; a parser is not thread-shareable by design. Catalog data point, not for individual filing. |

## Borderline (needs a decision)

| id | what | disposition |
|----|------|-------------|
| **TSAN-0010** | `marshal.dumps(shared_list)` reads `ob_item[]` while another thread `append`s | **read-while-mutate of a shared builtin** — same class as the suppressed `bytes_join`/`binarysort`. Arguments for fixing: the set-branch of the *same* `w_complex_object` already takes a critical section (missed conversion), and `list_resize` makes it a latent UAF. Arguments against: needs concurrent mutation of a shared list (unusual). Flagged. |

## Related non-race catalog entries

| id | what | upstream |
|----|------|----------|
| TSAN-0012 | faulthandler `is_enabled()` reads `fatal_error.enabled` that `enable()`/`disable()` write | already reported **#151363** |

## Open dev-questions (not individual races)

See `notes/open-questions-for-umbrella.md`: **Q1** concurrent `list.sort()` of a shared list
(`binarysort`, no critical section — is it meant to stay crash-safe?); the tzset/mktime glibc/TSan
false positive (resolved, not a bug).

## Cross-check

None of these overlap **#149816** ("22 free-threading race conditions") — that umbrella covers
entirely different modules (`_random`/`_ssl`/`typeobject`/`listobject`/`_pickle`/`dict`/`bytes`/
`memoryview`/`_struct`/`_ctypes`/`_elementtree`/`bufferedio`).
