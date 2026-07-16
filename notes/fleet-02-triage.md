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
- **StringIO** `close|iternext` (9), `realize|realize` (1) → **TSAN-0007** area (unguarded `stringio_iternext`, #153296/PR#153368). TODO: confirm the fix's critical section also covers `close`.
- **pyexpat** `XML_Parse|XML_Parse` (4), `SetReparseDeferralEnabled` (1), `poolGrow|setContext` (1) → **TSAN-0009** (bundled single-threaded libexpat).
- **locale** `setlocale`/`decode_monetary` (3) → **#127081** "Thread-unsafe libc functions" (open; also conceptually covers the tzset glibc-FP). Known libc thread-unsafety, not CPython's to fix per-call.

## Out of scope

- **Subinterpreters** (per **#143232** policy): `PyInterpreterState_Head|_PyInterpreterState_New` (8, concurrent_interpreters), `posixmodule_exec` (6, _interpreters), `structseq_new_impl` (2, _interpchannels), `_PyExc_InitTypes` (1). ~17 vehicles excluded.
- **Concurrent `__init__`/construction** (per **#127192**, unsupported): `bytearray___init___impl|…` (2) — concurrent re-init of a shared bytearray. Excluded. (tp_new_wrapper under investigation — see TSAN-0020, scope TBD.)

## Genuinely-new deep-dive (in flight — TSAN-0015..0021, 7 parallel agents)

- **TSAN-0015** odict `_odict_clear_nodes | odictiter_new` (4) — OrderedDict clear-vs-iterate, likely UAF.
- **TSAN-0016** readline `get_completer | set_hook` (7) — readline global state (assess CPython-state vs libreadline-global).
- **TSAN-0017** _zstd `flush | flush` (8) — concurrent flush; distinct-from-vs-dup-of TSAN-0002 (last_mode) TBD.
- **TSAN-0018** typeobject `object_getstate_default` vs atomic ssize store (3) — `__getstate__` (shelve/pickle).
- **TSAN-0019** _decimal `Context_clear_flags | context_repr` (3) — Context flags; cross-check **#149142** (mpd_context_t.status, devdanzin) — likely dup.
- **TSAN-0020** typeobject `tp_new_wrapper | tp_new_wrapper` (4) — SCOPE TBD (concurrent construction #127192 vs shared type-state).
- **TSAN-0021** dictobject `clear_lock_held | clear_lock_held` (1) — dict clear under critical section; why does TSan still flag it (shared empty-keys singleton?).

## Remaining new singles not yet assigned (candidates for a later pass)

`_elementtree XMLParser__setevents` cluster (setevents|setevents ×2, |list_resize, |_Py_atomic_store_ptr_release) — concurrent parser `_setevents`; `dict dictiter_iternext_threadsafe`; `subtype_getweakref` (weakref); `fileio close|_Py_fstat`, `FileIO __init__|_Py_fstat`, `epoll close|fileno` (fd/fstat lifecycle); `frame_trace_opcodes_set|trace_trampoline`; `monitoring_use_tool_id`; `add_subclass|clear_tp_subclasses`; `rlock_repr`; `cfunction_vectorcall_NOARGS` (2, = the tzset glibc FP, still un-suppressible via generic signature).
