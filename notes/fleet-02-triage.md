# Fleet 02 triage (fusil-tsan_fleet_02, 2026-07-15)

5 instances, 290 crash dirs ingested vs the catalog. **Known races deduped cleanly:** TSAN-0013 (50),
0001 (25), 0004 (14), 0014 (9), 0007 (8), 0006 (7), 0009 (7), 0005 (7), 0002 (3), 0012 (3), 0010 (3),
0008 (1); + 11 suppressed (glibc FPs), 2 framework, 24 noparse. **45 new signature groups** surfaced.

## Folded into existing entries (signature variance — committed 5c38514)

- `_Py_atomic_compare_exchange_ssize | count_repr` (4) → **TSAN-0006**
- `list_resize | w_complex_object` (1) → **TSAN-0010**
- `hz_decode_reset | _PyLong_FromByteArray` (5) → **TSAN-0004** (HZ codec, same cjkcodecs class)

## Known / already-dispositioned families (not for individual filing)

- **tracemalloc** — `PyMem_SetAllocator` vs `PyObject_Malloc`/`PyMem_Free`/`PyMem_RawMalloc`/… (5 sigs,
  ~12 vehicles) = `tracemalloc.start()/stop()` swapping the allocator under concurrent allocation.
  Known: devdanzin **#126315** ("tracemalloc aborts from threads in no-gil") / by-design.
- **faulthandler** `disable | is_enabled_impl` (3) → **TSAN-0012** (`fatal_error.enabled`, #151363/#151475).
- **_lsprof** `Stop|flush_unmatched` (3), `flush_unmatched|initContext` (3) → **TSAN-0008** (gh-116738/#138229 teardown residual).
- **StringIO** `close|iternext` (9), `realize|realize` (1) → **TSAN-0007** area (unguarded `stringio_iternext`, #153296). CONFIRMED: PR #153368 (wraps `stringio_iternext` in `Py_BEGIN_CRITICAL_SECTION(self)`) is still **OPEN/unmerged**, so current main's `iternext` is unguarded while `close` (a clinic method) is guarded → they race; the pending fix covers `close|iternext` too (both take `self`'s critical section). No new entry.
- **pyexpat** `XML_Parse|XML_Parse` (4), `SetReparseDeferralEnabled` (1), `poolGrow|setContext` (1) → **TSAN-0009** (bundled single-threaded libexpat).
- **locale** `setlocale`/`decode_monetary` (3) → **#127081** "Thread-unsafe libc functions" (open; also conceptually covers the tzset glibc-FP). Known libc thread-unsafety, not CPython's to fix per-call.

## Out of scope

- **Subinterpreters** (per **#143232** policy): `PyInterpreterState_Head|_PyInterpreterState_New` (8, concurrent_interpreters), `posixmodule_exec` (6, _interpreters), `structseq_new_impl` (2, _interpchannels), `_PyExc_InitTypes` (1). ~17 vehicles excluded.
- **Concurrent `__init__`/construction** (per **#127192**, unsupported): `bytearray___init___impl|…` (2) — concurrent re-init of a shared bytearray. Excluded. (tp_new_wrapper under investigation — see TSAN-0020, scope TBD.)

## Deep-dive RESULTS (TSAN-0015..0021 — all reproduced exit 66, independently re-verified)

- **TSAN-0018 (+0021) = the one genuinely-NEW bug: shared split-keys `dk_nentries` non-atomic
  readers.** `object.__getstate__` (`object_getstate_default`, via pickle/copy) AND `dict.clear`
  (`clear_lock_held`, on a sibling instance of the same class) read a **type's shared split-keys
  `dk_nentries`** with a plain load, racing `setattr`'s atomic bump (`split_keys_entry_added`, which
  is deliberately atomic — comment "when we're racing with reads"). The atomic macro
  `LOAD_KEYS_NENTRIES` already exists (`dictobject.c:237`) and is used correctly elsewhere (`:4632`)
  — these readers just forgot it. Low severity (value-benign) but real, in scope, **no existing
  filing**. Fix: `LOAD_KEYS_NENTRIES` at the reader sites. 0021 folded into 0018 (two reader faces,
  one bug). Cross-check gh-116738 before filing.
- **TSAN-0015 odict** clear-vs-iterate → **ALREADY REPORTED #151627** (UAF in `odictiter_new`, PR
  #151688 pending). Real, confirmed; reader path unlocked while clear is `@critical_section`.
- **TSAN-0016 readline** `get_completer` → **ALREADY REPORTED #153291** (PR #153362; covers the
  sibling `get_pre_input_hook` too). Real CPython module-state race (not libreadline), confirmed.
- **TSAN-0019 decimal Context** `clear_flags | repr` → **DUP of #149142** (mpd_context_t.status,
  PR #150598 pending). Not for separate filing.
- **TSAN-0017 _zstd flush** → **DUP of TSAN-0002** (same `last_mode`; control experiment with no
  getattr runs clean — no distinct cctx bug). Folded into 0002.
- **TSAN-0020 tp_new_wrapper** → **OUT OF SCOPE**: the racing memory is inside OpenSSL's libcrypto
  (each thread builds its own `ssl.SSLContext`; `tp_new_wrapper` is just the nearest symbolized
  frame — libcrypto is stripped). An OpenSSL-internal cache race, like TSAN-0003. Kept OUT of
  known_races (signature too broad); documented.

**Net from the 7 deep-dives: 1 genuinely-new filable bug (TSAN-0018 split-keys readers), 3 already
reported (#151627/#153291/#149142), 1 dup of a catalog entry (0002), 1 out-of-scope (OpenSSL).**

## Remaining new singles not yet assigned (candidates for a later pass)

`_elementtree XMLParser__setevents` cluster (setevents|setevents ×2, |list_resize, |_Py_atomic_store_ptr_release) — concurrent parser `_setevents`; `dict dictiter_iternext_threadsafe`; `subtype_getweakref` (weakref); `fileio close|_Py_fstat`, `FileIO __init__|_Py_fstat`, `epoll close|fileno` (fd/fstat lifecycle); `frame_trace_opcodes_set|trace_trampoline`; `monitoring_use_tool_id`; `add_subclass|clear_tp_subclasses`; `rlock_repr`; `cfunction_vectorcall_NOARGS` (2, = the tzset glibc FP, still un-suppressible via generic signature).
