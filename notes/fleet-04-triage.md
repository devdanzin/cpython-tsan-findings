# Fleet 04 triage (fusil-tsan_fleet_04, 2026-07-16)

6 instances, **1585 crash dirs**. Restarted mid-run (to pick up fusil's process-group reaping fix,
#208), and the **first fleet to run with the fixed `tsan_dedup` parser** (#207) — so the in-loop
labels are trustworthy for the first time, and **0 dirs were labeled `tsanFRAME`** (the
false-positive class is gone in the wild, not just in tests).

> **Ingest-glob correction.** The documented command was
> `ingest.py <fleet-dir>/inst-*/python/*` (fusil `fleet/README.md`), which is **wrong for any
> restarted fleet**: a restarted instance gets a *fresh* project directory beside the first one
> (`python-2`, `python-3`, …), so that glob only ingests the pre-restart run. Here it covered
> **155 of 1585 dirs (10%)** and made the fleet look far cleaner than it was. Use
> `inst-*/python*/*`. The README is fixed. Re-checked the earlier fleets with the correct glob:
> fleet-01 40/40 (nothing missed), fleet-02 291→296, fleet-03 917→957 — **both still ingest 0 new
> signature groups**, so those conclusions stand unchanged.

## Result

1585 dirs → **25 known races deduped** (~800 vehicles) + 585 suppressed + 191 noparse, and **25 new
signature groups → 2 after triage**. Nearly all the "new" groups were additional *faces* of entries
we already hold — the catalog is converging.

Known-race tally: TSAN-0001 (56), 0013 (50), 0008 (48), 0009 (40), 0007 (39), **0031 (38)**, 0019 (38),
0005 (36), 0015 (35), 0012 (34), 0002 (33), 0006 (33), 0014 (33), 0030 (32), 0004 (32), 0028 (31),
0010 (31), **0018 (31)**, 0016 (28), 0026 (28), 0023 (27), 0024 (24), 0025 (19), 0029 (9), **0034 (2)**.

## Folded — new faces of existing entries (23 groups)

- **TSAN-0008** (lsprof teardown): `_lsprof_Profiler__ccall_callback_impl|_PyMem_DebugRawFree`,
  `…__creturn_callback_impl|…`, `RotatingTree_Get|_PyMem_DebugRawFree`, `Stop|initContext`,
  `clearEntries|getEntry`.
- **TSAN-0009** (shared pyexpat parser): `PyExpat_XML_Parse|PyExpat_XML_ParseBuffer`,
  `PyExpat_XML_GetBuffer|PyExpat_XML_GetBuffer`, `PyExpat_XML_Parse|callProcessor`, `setContext|setContext`.
- **TSAN-0019** (decimal context, #149142): `_decimal_Context_clear_flags_impl|context_copy`,
  `context_repr|dec_addstatus`, `_decimal_Decimal_quantize_impl|dec_addstatus`.
- **TSAN-0031** (shared TreeBuilder): three more `treebuilder_*` pairs — incl. the self-pairs
  `treebuilder_extend_element_text_or_tail|…` and `treebuilder_handle_start|…`.
- **TSAN-0026** (dictiter vs dictresize): `get_index_from_order` ×2 — the reader is
  `get_index_from_order` called *from* `dictiter_iternext_threadsafe:6058`, racing `dictresize`'s
  atomic `ma_values` publish via `set_values:215`. Same bug as 0026's `:6043` site.
- **TSAN-0029** (frame trace-control): `frame_trace_opcodes_set_impl|call_trace_func`.
- **TSAN-0030** (monitoring tool registry): `monitoring_free_tool_id_impl|monitoring_use_tool_id_impl`.
- **TSAN-0007** (StringIO): `_io_StringIO_read_impl|_stringio_readline`.
- **TSAN-0024**: `pyepoll_internal_close|select_epoll_poll_impl` — the **`poll` face the report
  predicted** (0024 already held the `fileno` face and stated `fileno`/`register`/`poll` are unguarded).

## TSAN-0018 WIDENED — a third dk_nentries reader, and it's the public C-API

`_PyDict_Next` (5 vehicles) turned out to be TSAN-0018's exact defect with a new, more significant
reader:

```c
// reader -- Objects/dictobject.c:3170, _PyDict_Next
Py_ssize_t n = mp->ma_keys->dk_nentries;                          // PLAIN read
// writer -- Objects/dictobject.c:1911, insert_combined_dict
STORE_KEYS_NENTRIES(mp->ma_keys, mp->ma_keys->dk_nentries + 1);   // ATOMIC store
// the correct macro exists and is used correctly elsewhere:
:237   #define LOAD_KEYS_NENTRIES(keys) _Py_atomic_load_ssize_relaxed(&keys->dk_nentries)
:4632  for (i = 0; i < LOAD_KEYS_NENTRIES(a->ma_keys); i++) {
```

This is broader than 0018's original *split-keys* framing: `dk_nentries` is written atomically by
**both** writers (`split_keys_entry_added` for split keys, `insert_combined_dict` for combined) and
read plainly at several sites. The new face matters most because the reader is the **public
`PyDict_Next` C-API** (seen here via `_PyType_GetSubclasses`), so any C extension iterating a shared
dict hits it. Exactly the audit 0018's own "audit other plain iterations of dk_nentries" called for.
Entry retitled and rescoped; 3 reader faces now.

## Suppressed

- `posixmodule_exec|count_members` — another subinterpreter-machinery pairing (#143232).
- `cfunction_vectorcall_NOARGS|tp_new_wrapper` — **the OpenSSL FP again** (confirmed:
  `memcmp <- libcrypto.so.3 <- tp_new_wrapper`), but with the two stanzas collapsing to *different*
  generic frames, so the existing both-same anchored suppression missed it. This is the second
  workaround of that kind: the real fix is the pending `parse_report` change to keep the innermost
  non-interceptor libc/foreign frame, otherwise each new generic-frame permutation needs its own line.

## The 2 genuinely-new findings (both reproduced, both NEW)

**TSAN-0035 — shared `socket`: `sock_timeout` is plain-read/plain-written.**
`sock_setblocking` writes `s->sock_timeout` (`socketmodule.c:3172`), `sock_gettimeout_impl` reads it
(`:3308`); **all 15 accesses in the file are plain** — no atomics, no critical section. Incomplete FT
conversion, with hard evidence: the sibling field `sock_fd` **on the same struct** got relaxed-atomic
accessors (`get_sock_fd`/`set_sock_fd`, `:567-592`) via gh-128277/PR#128304, and `state->defaulttimeout`
— *the same `PyTime_t` type* — was made atomic via gh-116616/PR#116623. `socketmodule.c:1134` holds the
asymmetry in one statement: it atomically loads the default and **plain-stores** it into `s->sock_timeout`.
Reproduced **10/10**. **NEW:** gh-128277 "Make socket module thread safe" is CLOSED and was opened
*because of TSan warnings on this module*, yet none of its three merged PRs touches `sock_timeout`
(verified by diffing them). Note **gh-116738 already ticks `- [x] Modules/socketmodule.c`** — this shows
that checkbox is premature. `_ssl.c` reads `sock->sock_timeout` directly in four plain accesses
(`:453/:839/:1024/:2543`), which is how the `ssl` vehicle surfaced it, so `_ssl` inherits the race.
Severity **LOW, honestly**: two impact stories were investigated and *refuted* — the `gettimeout()`
double-read is coalesced into one load by the compiler (disassembly + 10k-call probe: 0 violations),
and the compound-state hazard (timeout field vs the FD's `O_NONBLOCK`) showed 0 divergences in 28,800
racing pairs. Benign as compiled, still formally C11 UB. Fix: `get_sock_timeout`/`set_sock_timeout`
mirroring the `sock_fd` pair.

**TSAN-0036 — instrumentation: `active_monitors.tools[]` read lock-free by the eval loop.**
`no_tools_for_local_event` (inlined into `_PyEval_NoToolsForUnwind`, `ceval.c:2465`, via `gen_close`)
does a plain 1-byte load of `code->_co_monitoring->active_monitors.tools[event]`, holding no lock and
reading no version; `force_instrument_lock_held` (`instrumentation.c:1842`) replaces the whole
`active_monitors` struct (two 8-byte stores) under `LOCK_CODE` only. Addresses confirm the overlap
arithmetically: the 8-byte store at `…528` covers `tools[8..15]`, the 1-byte load at `…52d` is
`tools[13]` = `PY_MONITORING_EVENT_PY_UNWIND`. **`LOCK_CODE` cannot help** — it's a per-code critical
section the eval loop never takes; registration *is* STW, but STW only re-instruments *executing* code,
and everything else is re-instrumented **lazily** from the `RESUME` version check with the world
running — that lazy path is the racing writer. Incomplete conversion, provable: the file has 13
`FT_ATOMIC_*` uses on exactly the state the eval loop reads lock-free (incl. a deliberate
`STORE_RELEASE`/`LOAD_ACQUIRE` pairing on `_co_instrumentation_version`), but this reader never
acquire-loads the version, and no accessor of `active_monitors.tools[]` uses atomics at all. Line 1842
is **unchanged since the original PEP 669 commit (2023)** — it predates free-threading. Reproduced
**6/6**. **NEW, with strong prior art: gh-136870** (closed, filed from coverage.py under TSan) is the
*same root pattern in the same file*, and its fix **PR #136994** converted four `LOCK_CODE` sites to
`_PyEval_StopTheWorld` **precisely because `LOCK_CODE` doesn't exclude lock-free eval-loop readers** —
but only for the *bytecode* tool bytes; it never touched `active_monitors`. **This is the sibling that
fix missed** — that's the framing to file under. Distinct from TSAN-0030 (tool-id *registry* TOCTOU)
and TSAN-0029 (per-frame `f_trace`): three different fields, writers and fixes. Severity **LOW**
(value-benign; worst case a missed/spurious monitoring event during (de)registration). Fix:
`FT_ATOMIC_LOAD_UINT8_RELAXED` in the reader + a per-byte relaxed-atomic store loop at :1842 —
notably #136994's `LOCK_CODE → StopTheWorld` upgrade is a *poor fit* here, since `_Py_Instrument` is
called *from* the eval loop and its `LOCK_CODE` asserts `!world_stopped`.

## Search-tooling caveat found while triaging this fleet

`gh search issues --repo X "a b"` **silently under-reports multi-word queries** (phrase vs AND
semantics) — e.g. `monitoring free-threading` returns **0** via `gh search` but **32** via
`gh api -X GET search/issues`. Any "NEW" verdict resting on `gh search` returning empty is untrustworthy.
The investigation kit now mandates `gh api`. Re-verified **TSAN-0033**'s verdict with `gh api`: it
stands. That re-check did surface **#142975** (CLOSED/fixed) — *same* `validate_refcounts` assertion
text but a different producer (`gc.freeze`/`gc.unfreeze`, object type `method`). `validate_refcounts` is
a **generic detector**: the `object type name` line is the discriminator. Recorded on TSAN-0033 so
#153809 isn't closed as a dup on the strength of shared assertion text.

## noparse bucket (191)

- **160 → TSAN-0033** (`validate_refcounts` / `_asyncio.Task`). By a wide margin the most prolific
  finding of the fleet, and independent corroboration of the fleet-03 filing.
- **31 benign**: 12 watchdog sigkills, 6 sigterms, 12 in-flight `session-NNN` dirs (fleet stopped),
  1 misc.

## Counts

1585 dirs = ~800 known-race vehicles (25 races) + 585 suppressed + 2 new (TSAN-0035/0036 pending)
+ 191 noparse (160 = TSAN-0033, 31 benign).
