# magalu remote fleets triage (2026-07-19)

Two downloaded `--tsan-no-halt` fleets from a remote box — `~/crashers/magalu_tsan/{fleet_tsan01,
fleet_tsan02}` (852 crash dirs, 192 sidecars), built on `/home/ubuntu/projects/3.16_ft_debug_tsan`
(same 3.16 free-threaded `--with-thread-sanitizer` version as ours, different machine). A third,
`magalu03_tsan01.tar.zstd`, is still compressed (not analyzed).

## Net: no slam-dunk new bug (as expected) — but a few new-but-low-value races + a known crash

These fleets are **count-heavy** (TSAN-0006 = 785 vehicles) and exercised modules our campaign
fleets hit less (`_elementtree.XMLParser`/expat, `pickle`, `cProfile`, t-strings), so they surfaced
those modules' shared-object races. ~22 new signature groups → **4 minted, 5 folded, 1 suppressed,
11 left uncataloged** (count-cascade generic faces + build-specific SEGVs).

### Minted

- **TSAN-0049 — `_lsprof` profiler use-after-free = cpython#126884 (CLOSED).** A `Profiler` is freed
  (`profiler_dealloc`, via a traceback dealloc chain) while another thread's instrumentation call
  event still invokes it (`ptrace_enter_call → getEntry → RotatingTree_Get`) → UAF → the
  `RotatingTree_Get` SEGV. **Already filed** ("Calling `cProfile.runctx` in threads on a
  free-threading build segfaults"). Object-lifetime UAF, like the fleet-12 socket race. Not a new
  filing. (repro mirrors the shape; the specific dealloc-while-active window wasn't hit in a quick
  isolated run.)
- **TSAN-0050 — `_elementtree.XMLParser` / pyexpat shared-parser race (NEW, reproduced).** Concurrent
  `feed()/flush()/close()` on one shared parser race libexpat's single-threaded `XML_Parser` state
  (no CPython-side lock). Low priority — a parser is inherently sequential; sharing it is misuse.
  Same class as the thread-unsafe-libc `localeconv` (TSAN-0047 / #127081). Appears unfiled.
- **TSAN-0051 — `pickle.PickleBuffer.raw()` vs `.release()` race (NEW, reproduced).** Concurrent read
  of the view vs its `PyBuffer_Release`. Low priority (unusual sharing). Appears unfiled.
- **TSAN-0052 — t-string `templateiter_next` shared-iterator race (NEW, value-benign).** The PEP 750
  template iterator's `from_strings` flag + sub-iterator cursors, unsynchronized. gh-120496/gh-124397
  value-benign class (the `Py_SETREF` is on a local, so no double-free like `ga_iternext`).
  Crash-checked negative. Not fileable. (Didn't trip in a quick isolated repro — a short template
  exhausts before workers contend.)

### Folded

- `_thread_RLock__acquire_restore_impl | rlock_repr` + `rlock_repr | _PyRecursiveMutex_TryUnlock`
  → **TSAN-0028** (RLock repr).
- `treebuilder_extend_element_text_or_tail | treebuilder_handle_data` → **TSAN-0031** (TreeBuilder).
- `create_extra | element_bool` → **TSAN-0041** (Element.extra).
- `long_to_decimal_string_internal` self-race + `_PyLong_DigitCount | _PyUnicode_ResizeCompact`
  → **TSAN-0006** (count slow-mode; count confirmed on the stack for both).

### Suppressed

- `new_interpreter | _PyInterpreterState_IsReady` — FT subinterpreter machinery, out of scope per
  cpython#143232.

### Left uncataloged (11 — count-cascade / generic)

The count slow-mode UAF (785 veh) produces many generic downstream faces when it reprs a freed
long: `_Py_atomic_load_ssize_relaxed | _PyMem_DebugRawFree` (4), `_PyMem_DebugRawFree` self-race,
`PyFloat_AS_DOUBLE | _PyMem_DebugRawAlloc`, several `PyUnicode_GET_LENGTH/IS_ASCII | long_alloc /
_PyMem_DebugRawAlloc`, and build-specific SEGVs (`pc…4bec`, `SEGV PySequence_GetItem /
PyObject_GetOptionalAttr / PyType_HasFeature`). All too generic to fold without mislabeling risk (or
build-specific pc), so documented and left — the same call made for the fleet-11/12 count leftovers.

## Catalog

`known_races.tsv`: 166 → **182 signatures / 45 → 49 races**. Re-ingest of the magalu fleets drops
new groups 22 → 11 (the count-cascade leftovers above).

## Takeaway

Consistent with "probably nothing new": the only memory-unsafe crash is already filed (#126884), and
the three genuinely-new races (XMLParser/expat, PickleBuffer, template iterator) are low-priority
shared-object / value-benign classes. The value of the download was breadth — modules our fleets
under-exercise — confirming those areas hold only the expected shared-object-not-thread-safe races.
(TSAN-0049 and TSAN-0052 are meta + repro only — known/non-fileable, so no standalone report; 0050
and 0051 have reports.)
