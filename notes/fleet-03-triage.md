# Fleet 03 triage (fusil-tsan_fleet_03, 2026-07-16)

6 instances, **917 crash dirs** ingested vs the catalog. Overnight run on the same
`debug-ft-nojit-tsan` build. Baseline going in: 49 known signatures / 27 races + the suppressed
families. Result: **the catalog deduped the overwhelming bulk**; only **two genuinely-new clusters**
survived triage.

## Headline

- **23 known races deduped cleanly** (TSAN-0001/0002/0004/0005/0006/0007/0008/0009/0010/0011/0012/
  0013/0014/0015/0016/0018/0019/0024/0025/0026/0028/0029/0030). Nine of them capped at 30 vehicles
  (the per-signature sample cap), i.e. they'd have matched far more.
- **42 "new" signature groups on first pass → 9 after fold/fix → 2 real new findings.** The drop came
  from: (a) fixing two mis-ordered catalog signatures, (b) folding ~25 signature-variance faces into
  existing entries, (c) suppressing three known FP/out-of-scope families.
- **NEW this fleet: TSAN-0031 (elementtree TreeBuilder shared internal state) and TSAN-0032
  (io.BufferedReader.detach missing critical section).** Both reproduced (see their report dirs).

## Tooling bug #2 (the big one): `Previous atomic write` stanzas were dropped → fabricated `A | A` signatures

Checking the `tsanFRAME` bucket for false positives (all 10 turned out to be FPs) exposed a
**systematic parser bug** in fusil's `tsan_dedup.py` that had been silently corrupting signatures for
the whole campaign:

1. TSan capitalises the FIRST access line (`Read of size ...`) but **lowercases the second**, including
   the qualifier: `Previous atomic write of size ...`. The `ACCESS` regex only accepted a capital
   `Atomic`, so **`Previous atomic write` / `Previous atomic read` never matched**.
2. With the 2nd stanza unrecognised, `_parse_race` found only one stanza, and the
   `if len(stanzas) < 2: stanzas.append(stanzas[0])` fallback **duplicated the first** —
   fabricating a symmetric `A | A` signature.
3. That hit exactly the **non-atomic-reader-vs-atomic-writer** shape, which is the single most common
   race class in this catalog. **150 of fleet-03's 827 race dirs (18%)** were mis-parsed.
4. For RLock it also tripped the framework heuristic: both fabricated "sites" were `_threadmodule.c`,
   and `FRAMEWORK_FILES` matched that whole file — so a real race was labeled harness noise.

**Fixed in fusil** (branch `tsan-dedup-atomic-stanza-parse`, +4 regression tests, ruff clean):
- `ACCESS` now accepts `[Aa]tomic` → the 2nd stanza parses; `A | A` is no longer fabricated.
- `FRAMEWORK_FILES` narrowed from the whole `_threadmodule.c` to **`thread_pthread.h` + an explicit
  `FRAMEWORK_FUNCS` set** of thread-lifecycle entry points (`thread_run`, `ThreadHandle_start`,
  `do_start_new_thread`, …). Evidence: across all 3 fleets the ONLY `_threadmodule.c` functions that
  ever appear as *access-stanza sites* are the public RLock API (`rlock_repr` ×19,
  `_thread_RLock_acquire_impl` ×18, `_thread_RLock__release_save_impl` ×1); the lifecycle functions
  appear only in thread-*creation* stacks, which never feed a signature. So the file-level match
  produced 10 FPs and 0 true positives. (This also flips the old test fixture's premise: a race
  between public `_thread.lock`/RLock methods is a genuine finding, not scaffolding.)

**Catalog signatures re-derived from evidence.** With the parser fixed, old→new signatures were mapped
by re-parsing all 1248 dirs across fleets 01/02/03 both ways (1:1, no ambiguity), and the artifacts
replaced with true forms:

| race | artifact (unreachable now) | true signature |
|------|---------------------------|----------------|
| TSAN-0014 | `binarysort \| binarysort` (40 dirs) | `_Py_atomic_load_ptr \| binarysort` |
| TSAN-0014 | `sortslice_copy_decr \| sortslice_copy_decr` | `_Py_atomic_load_ptr \| sortslice_copy_decr` |
| TSAN-0006 | `count_repr \| count_repr` (32) | `_Py_atomic_compare_exchange_ssize \| count_repr` (already held) |
| TSAN-0013 | `_Py_SIZE_impl \| _Py_SIZE_impl` (31) | `_Py_atomic_store_ssize_relaxed \| _Py_SIZE_impl` (already held) |
| TSAN-0010 | `w_complex_object \| w_complex_object` (25) | `_Py_atomic_store_ptr_release \| w_complex_object` (already held) |
| TSAN-0002 | `_zstd..flush_impl \| _zstd..flush_impl` (18) | `_Py_atomic_load_int_relaxed \| _zstd_ZstdCompressor_flush_impl` |
| TSAN-0023 | `subtype_getweakref \| subtype_getweakref` (7) | `_Py_atomic_store_ptr \| subtype_getweakref` |
| TSAN-0018 | `clear_lock_held \| clear_lock_held` (3) | `_Py_atomic_store_ssize_relaxed \| clear_lock_held` |
| TSAN-0026 | `dictiter_iternext_threadsafe \| …` | `_Py_atomic_store_ptr_release \| dictiter_iternext_threadsafe` (already held) |
| TSAN-0028 | (ex-`tsanFRAME`) | + `_thread_RLock__release_save_impl \| rlock_repr` |
| TSAN-0009 | `_setevents_impl \| _setevents_impl` | resolves to TSAN-0013's shared-list signature → removed from 0009 |

Two substantive corrections fell out of this:

- **TSAN-0014's `binarysort | binarysort` was itself an artifact.** The true form
  `_Py_atomic_load_ptr | binarysort` is a *reader's atomic load* racing binarysort's plain in-place
  write — independently confirming the **sort-vs-READ** story that shrinkray established empirically,
  and explaining why the original "two concurrent sorters" reading of it never reproduced.
- **A real bug was being suppressed.** The `bytearray___init___impl | bytearray___init___impl`
  suppression (added as "concurrent `__init__`, out of scope per #127192") was an artifact: those 10
  vehicles truly are `_Py_atomic_store_ptr_release | bytearray___init___impl`, i.e.
  `bytearray(shared_list)` reading the source list while another thread appends — a shared-**list**
  reader face of **TSAN-0013**, which is a real bug under the Yhg1s ruling. Suppression **withdrawn**;
  those 8 fleet-03 dirs now correctly label TSAN-0013 (35→43).

After the fix + migration: all 3 fleets re-ingest clean — fleet-03 **0 new groups, 0 framework**,
fleet-02 0 new, fleet-01 only the known pty/TSan-runtime SEGV (`addr=0xd8`, resolved by fusil #205).

## Tooling bug #1 found + fixed: mis-ordered catalog signatures silently failed to dedupe

`fusil tsan_dedup.parse_report` emits the race signature as a **sorted** `file:func | file:func`
pair, and the deduper matches it **exactly** against `known_races.tsv`. But two `meta.json` files
stored their signature pair in the *other* order, so `gen_known_races.py` (which copied verbatim)
wrote a row that could never match a live report:

- **TSAN-0013**: `_setevents_impl | _Py_atomic_store_ptr_release` (should be `_Py_atomic… | _setevents…`)
- **TSAN-0029**: `trace_trampoline | frame_trace_opcodes_set_impl` (should be `frame_… | trace_trampoline`)

Both re-surfaced as bogus "NEW" groups in this fleet. Fixed the two metas **and** hardened
`gen_known_races.py` to normalize (sort) each pair on write, so a mis-ordered meta can't silently
break dedup again. (fusil-side proper fix — keeping the innermost libc frame so libc races get a
specific suppressible signature — is still pending; see below.)

## Folded into existing entries (signature variance — same bug, different call-site/frame collapse)

Added the observed signature strings to each entry's `signatures[]` so future fleets dedupe them:

- **TSAN-0009** (pyexpat, don't-share-parser): `expat_malloc|expat_malloc`, `xmlparse_handler_setter`
  (both), `PyExpat_XML_GetBuffer|PyExpat_XML_GetCurrentColumnNumber`, `…|…GetCurrentLineNumber`,
  `xmlparse_buffer_text_setter|getset_set`. All = concurrent method calls / attr-sets on ONE shared
  pyexpat parser (mostly inside bundled single-threaded libexpat). +11 vehicles (30→41).
- **TSAN-0019** (decimal context, dup of **#149142**): the whole decimal cluster —
  `dec_addstatus|dec_addstatus`, `dec_addstatus|dec_as_long`,
  `_decimal_Decimal_to_integral_value_impl|dec_addstatus`, `_decimal_Context_clear_traps_impl|context_repr`,
  `…|context_copy`. All = shared `mpd_context_t` status/traps mutated non-atomically (`dec_addstatus`
  during `Decimal(...)` arithmetic/construction, `Context.clear_traps`/`repr`/`copy`). Same class as
  #149142 (PR #150598 pending). +10 vehicles.
- **TSAN-0013** (shared-list non-atomic readers): `stringlib_bytes_join|stringlib_bytes_join`
  (b"".join of a shared list; the both-same collapse is still the join-reader-vs-resize class),
  `bytearray___init___impl|list_resize` and its `…|_Py_atomic_store_ptr_release` variant
  (`bytearray(shared_list)` reading the source list while another thread appends). NOT the #127192
  concurrent-init case — it's a shared-**list** reader. +5 vehicles.
- **TSAN-0029** (frame trace-control): `frame_trace_opcodes_set_impl|sys_trace_instruction_func`
  and `_Py_atomic_store_char_relaxed|_Py_call_instrumentation_line` (the `frame.f_trace_lines`
  member-set racing the eval-loop instrumentation read — a sibling face already anticipated in 0029's
  notes). Plus the order-fix face `frame_…|trace_trampoline`.
- **TSAN-0030** (sys.monitoring tool registry): `monitoring_get_tool_impl|monitoring_use_tool_id_impl`
  (get_tool reader vs use_tool_id writer on interp-global `monitoring_tool_names[]`).
- **TSAN-0012** (faulthandler enabled flag, #151363): `faulthandler_disable|faulthandler_py_enable_impl`,
  `faulthandler_disable_py_impl|faulthandler_enable`.
- **TSAN-0024** (FileIO fd lifecycle, #151707): `internal_close|_Py_read`, `internal_close|posix_do_stat`.
- **TSAN-0008** (lsprof teardown residual, gh-116738/#138229): `getEntry|RotatingTree_Enum`,
  `ptrace_leave_call|_PyMem_DebugRawFree`.
- **TSAN-0028** (RLock repr, **fixed #153292**/PR#153299): `rlock_repr|_PyRecursiveMutex_LockTimed`.
- **TSAN-0018** (split-keys dk_nentries readers): `object_getstate_default|object_getstate_default`.
- **TSAN-0026** (dict iter ma_values): `dictiter_iternext_threadsafe|dictiter_iternext_threadsafe`.
- **TSAN-0014** (concurrent list.sort): `sortslice_copy_decr|sortslice_copy_decr`.
- **TSAN-0010** (marshal shared-list reader): `_Py_atomic_store_ptr_release|w_complex_object`.

## Suppressed (known FP / out-of-scope families) — added to `catalog/suppressions.txt`

- **tzset glibc false positive** (23 vehicles, dominant): `cfunction_vectorcall_NOARGS|cfunction_vectorcall_NOARGS`.
  Confirmed by reading a vehicle: `#0 free → #1 tzset_internal time/tzset.c:401 → #2 cfunction_vectorcall_NOARGS`.
  `time.tzset()`/`mktime()` free+strdup `tzname[]` under glibc's uninstrumented `tzset_lock`. Same as
  TSAN-0003 / the earlier tzset repro. **Anchored** regex (both sides must collapse to the generic
  NOARGS frame) so a real NOARGS-cfunction race still surfaces.
- **OpenSSL foreign-lib false positive** (30 vehicles): `tp_new_wrapper|tp_new_wrapper`. Confirmed:
  `#0 memcmp → #1 libcrypto.so.3 → #2 tp_new_wrapper`. Concurrent `ssl.SSLContext()` construction
  races inside libcrypto's own cache; each thread builds its own context (see TSAN-0020). Anchored.
- **Subinterpreter machinery** (out of scope per **#143232**): `list_resize|structseq_new_impl`,
  `_Py_atomic_store_ptr_release|structseq_new_impl` (both from `_interpchannels`),
  `_PyInterpreterState_GetWhence|_PyInterpreterState_SetWhence` (`_interpreters`).

## Genuinely NEW (deep-dived, reproduced — see report dirs)

- **TSAN-0031 — `_elementtree.TreeBuilder` shared internal state.** ~8 signatures / ~18 vehicles: two
  threads feeding ONE shared TreeBuilder (`start`/`end`/`data`/`comment`/`pi`) race on its internal
  `this`/`last`/`data`/element-stack fields (`treebuilder_*` in `Modules/_elementtree.c`, no critical
  section). Distinct from TSAN-0009 (expat parser) and TSAN-0013/0022 (shared-list `_setevents`) — this
  is the TreeBuilder's OWN state. [disposition + issue-search verdict finalized on subagent report]
- **TSAN-0032 — `io.BufferedReader.detach()` missing `@critical_section`.** `_io__Buffered_detach_impl`
  plainly writes `self->raw=NULL; self->detached=1; self->ok=0` (`bufferedio.c:628`) with no lock,
  while its sibling clinic methods (`seekable`/`readable`/…) and `buffered_iternext` (via
  `_buffered_readline`) take `Py_BEGIN_CRITICAL_SECTION(self)`. Concurrent `.detach()` vs iterate on a
  shared BufferedReader = incomplete-CS-coverage race (class of TSAN-0007 StringIO). [finalized on
  subagent report]
- **TSAN-0033 — `_asyncio.Task` refcount-underflow / premature free (CRASH+HANG, not a data-race).**
  The single biggest non-race finding: **58 vehicles**, all identical —
  `Python/gc_free_threading.c:1083: validate_refcounts: Assertion "_Py_REFCNT(op) > 0" failed` on an
  `_asyncio.Task` (refcount 0 while still GC-tracked), during a concurrent `gc.collect()` while 4
  threads call methods on ONE shared Task. `TaskObj_dealloc` runs the finalizer + `unregister_task()`
  (a full `_PyEval_StopTheWorld` for a Task freed on a non-creator thread) **before**
  `PyObject_GC_UnTrack`. Labeled `tsanNOPARSE` by the deduper because it carries no TSan race stanza —
  it's a fatal abort.
  **RELEASE-BUILD CHECK (2026-07-16): NOT Py_DEBUG-only.** On `release-ft-nojit` (no Py_DEBUG, no
  sanitizer) the same repro **wedges the whole interpreter 5/5** at N=4/ITERS=40 — a thread spins at
  100% CPU forever inside the GC's mimalloc heap walk (`_mi_page_free_collect` ←
  `mi_heap_visit_blocks(update_refs)` ← `deduce_unreachable_heap` ← `gc_collect_main`) while every
  other thread parks on the runtime mutex; still wedged at 150s for work an identical-shape control
  finishes in 0.11s; reproduces down to 10 transient Tasks; a **segfault** (exit 139, core dumped) was
  also observed at ITERS=150. gdb stacks in the report's `release_hang_backtrace.txt`. So the Py_DEBUG
  assert is CPython catching the invariant violation *before* it becomes a release-build wedge —
  severity is HIGH, not debug-only. (release+TSan/ASan also time out on the same wedge, so neither
  sanitizer emits a report; the missing ASan UAF report is the wedge, not evidence of safety.)

## noparse bucket (90 dirs) — fully triaged

- **58 → TSAN-0033** (the `_asyncio.Task` refcount-underflow above). All 58 the same assertion/type.
- **20 = SIGKILL (signal 9), benign harness kills.** 17 are `lzma`/`compression.lzma` (LZMA compression
  is memory-heavy; under TSan's huge shadow reservation it trips the fusil CPU/mem watchdog or the
  OOM-killer) + `_elementtree`/`sched`/`asyncio_tools`. Not crashes. (The "abort" strings in session.log
  are just `abort_on_error=1`/`handle_abort=1` in the env dump, not real aborts — no self-destruct gap;
  fusil #205's `TSAN_UNSAFE_CALLS` exclusion holds.)
- **~11 = cpu_load SIGKILLs + early-killed `session-NNNN` dirs + swallowed worker exceptions.** Benign
  (e.g. `asyncio ... coroutine 'to_thread' was never awaited` → hung → killed; a fuzzer-passed `list`
  reaching `default_exception_handler` → AttributeError).
- **1 = SINGLETON to watch (not cataloged):** `threading-assertion` —
  `Python/generated_cases.c.h:13823: Assertion 'STACK_LEVEL() >= level' failed` in
  `_PyEval_EvalFrameDefault`, fired from a `WeakSet` weakref callback during concurrent execution. An
  eval-loop stack invariant; could be a real FT eval bug or exception-bomb/weakref-callback harness
  coupling. 1 occurrence, not reproduced — flagged here; revisit if it recurs in a later fleet.

## Counts (final, after the parser fix + signature migration)

917 dirs = **827 parseable-race dirs** (→ 24 known races deduped + 264 suppressed + **0 framework** +
the 2 new race clusters TSAN-0031 ×18 / TSAN-0032 ×1) + **90 noparse** (→ 58 = TSAN-0033 + 32
benign/singleton, above). **0 new signature groups remain.**

Pre-fix figures for reference (they appear in the earlier sections above): 272 suppressed, 10
framework, and several `A | A` signatures — all artifacts of the `Previous atomic write` parser bug.
