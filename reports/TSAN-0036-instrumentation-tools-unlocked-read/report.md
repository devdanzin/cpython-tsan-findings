# Data race: the eval loop reads `active_monitors.tools[]` with no lock while `_Py_Instrument` replaces the whole struct under the code object's critical section (`ceval.h` vs `instrumentation.c:1842`)

*`_PyEval_NoToolsForUnwind` (→ `no_tools_for_local_event`) does a plain 1-byte load of `code->_co_monitoring->active_monitors.tools[PY_MONITORING_EVENT_PY_UNWIND]` from `gen_close`, holding no lock. Concurrently, `force_instrument_lock_held` replaces that entire 16-byte `_Py_LocalMonitors` struct with a plain assignment, under `LOCK_CODE(code)` — which excludes other **writers** but not the lock-free eval-loop **reader**. This is the same "`LOCK_CODE` is not enough against eval-loop readers" defect that gh-136870 / PR #136994 fixed for the instrumented **bytecode** in this very file; the tools matrix was left behind.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`sys.monitoring` keeps, per code object, a matrix of which tools want which events:

```c
/* Include/internal/pycore_instruments.h:75-86 */
#define _PY_MONITORING_UNGROUPED_EVENTS 16

typedef struct _Py_LocalMonitors {
    uint8_t tools[_PY_MONITORING_UNGROUPED_EVENTS];
} _Py_LocalMonitors;

/* Include/internal/pycore_instruments.h:101-105 */
typedef struct _PyCoMonitoringData {
    _Py_LocalMonitors local_monitors;    /* offset  0, 16 bytes */
    _Py_LocalMonitors active_monitors;   /* offset 16, 16 bytes */
    ...
} _PyCoMonitoringData;
```

Two unsynchronized accesses to one code object's `active_monitors` race:

- **Write (plain, 16-byte struct assignment):** `force_instrument_lock_held` (`Python/instrumentation.c:1842`):
  ```c
  code->_co_monitoring->active_monitors = active_events;
  ```
  The compiler lowers this 16-byte copy to two 8-byte stores — one covering `tools[0..7]`, one covering `tools[8..15]`.

- **Read (plain, 1 byte):** `no_tools_for_local_event` (`Python/ceval.h`), inlined into `_PyEval_NoToolsForUnwind` (`Python/ceval.c:2465`):
  ```c
  static inline bool
  no_tools_for_local_event(PyThreadState *tstate, _PyInterpreterFrame *frame, int event)
  {
      assert(event < _PY_MONITORING_UNGROUPED_EVENTS);
      _PyCoMonitoringData *data = _PyFrame_GetCode(frame)->_co_monitoring;
      if (data) {
          return data->active_monitors.tools[event] == 0;   /* <-- plain 1-byte load */
      }
      else {
          return no_tools_for_global_event(tstate, event);
      }
  }
  ```

The TSan addresses confirm the overlap exactly. With `_PyCoMonitoringData` at `…510`, `active_monitors` sits at `…520`; the racing 8-byte store is at `…528` (covering `tools[8..15]`) and the racing 1-byte load is at `…52d` — i.e. `tools[13]`, which is `PY_MONITORING_EVENT_PY_UNWIND` (`Include/cpython/monitoring.h:34`). **The reader loads one byte out of the middle of the word the writer is replacing.**

## Why the lock doesn't help

The writer is called under the code object's critical section:

```c
/* Python/instrumentation.c:1953-1959 */
int
_Py_Instrument(PyCodeObject *code, PyInterpreterState *interp)
{
    int res;
    LOCK_CODE(code);                    /* Py_BEGIN_CRITICAL_SECTION(code) */
    res = instrument_lock_held(code, interp);
    UNLOCK_CODE();
    return res;
}
```

and `instrument_lock_held` / `is_version_up_to_date` / `force_instrument_lock_held` all assert `ASSERT_WORLD_STOPPED_OR_LOCKED(code)` (`:1938`, `:1066`). But `no_tools_for_local_event` — on the generator-close and exception-unwind paths — takes **nothing**. A per-object critical section only serializes participants that acquire it; the eval loop never does. So `LOCK_CODE` orders writer-vs-writer and leaves writer-vs-reader completely unsynchronized.

The registration entry points *are* properly stopped-the-world — `_PyMonitoring_SetEvents` asserts `ASSERT_WORLD_STOPPED()` (`:2035`) and calls `instrument_all_executing_code_objects` (`ASSERT_WORLD_STOPPED()`). The gap is the **lazy** path: STW re-instruments only the code objects that are *currently executing*. Every other code object is left with a stale `_co_instrumentation_version` and is re-instrumented later, by whichever thread next reaches its `RESUME`, with the world running and only `LOCK_CODE` held:

```c
/* Python/generated_cases.c.h:11385-11391 — the RESUME instrumentation check */
uintptr_t global_version = _Py_atomic_load_uintptr_relaxed(&tstate->eval_breaker) & ~_PY_EVAL_EVENTS_MASK;
uintptr_t code_version = FT_ATOMIC_LOAD_UINTPTR_ACQUIRE(_PyFrame_GetCode(frame)->_co_instrumentation_version);
if (code_version != global_version) {
    ...
    int err = _Py_Instrument(_PyFrame_GetCode(frame), tstate->interp);
```

That is the writer in both the fleet vehicle and the reproducer: a thread lazily re-instrumenting the *shared generator code object* while another thread reads the same object's `active_monitors` in `gen_close`.

## Is there an intended atomics discipline? Yes — and this field was left out of it

This is not a file that ignores atomics. `Python/instrumentation.c` has 13 `FT_ATOMIC_*` uses, and they are precisely the state the eval loop reads without a lock:

- the instrumented **opcodes**: `FT_ATOMIC_STORE_UINT8_RELAXED(*opcode_ptr, ...)` at `:700`, `:748`, `:778`, `:796`, `:825`, and `FT_ATOMIC_STORE_UINT8(instr->op.code, ...)` at `:723`;
- the **version word**, with an explicit release/acquire pairing — `FT_ATOMIC_STORE_UINTPTR_RELEASE(code->_co_instrumentation_version, global_version(interp))` at `:1926`, paired with the `FT_ATOMIC_LOAD_UINTPTR_ACQUIRE` in the RESUME check quoted above.

So the release-store of the version at `:1926` is deliberately sequenced *after* the `active_monitors` write at `:1842`. That pairing works for readers that acquire-load the version first. **`no_tools_for_local_event` never reads the version** — it dereferences `active_monitors` directly — so it never establishes the happens-before, and the publication protocol simply doesn't apply to it.

No reader of `active_monitors.tools[]` anywhere uses an atomic load, and no writer of it uses an atomic store: `:1160`, `:845`, `:880`, `:961`, `:1113`, `:1119`, `:604`, `:620` are all plain accesses, as is the `:1842` struct assignment. This is an **incomplete free-threading conversion**, not a locally-inconsistent one: the bytecode and the version were converted, the tools matrix that gates them was not.

`git log -L 1842,1842:Python/instrumentation.c` shows the line is unchanged since the original PEP 669 implementation (`411b1692811`, GH-103083) — it predates free-threading and was never revisited.

## Prior art: this is the bug gh-136870's fix stopped one step short of

Issue **gh-136870** ("data races in instrumentation when running coverage under TSAN", closed 2025-07-25) was the same root pattern. Its fix, **PR #136994** ("fix data races in instrumentation of bytecode"), changed four sites in this file from `LOCK_CODE` to stop-the-world, e.g.:

```diff
-                LOCK_CODE(code);
+                PyInterpreterState *interp = tstate->interp;
+                _PyEval_StopTheWorld(interp);
                 remove_tools(code, offset, event, 1 << tool);
-                UNLOCK_CODE();
+                _PyEval_StartTheWorld(interp);
```

That is an explicit acknowledgement by CPython that **`LOCK_CODE` is insufficient for instrumentation state the eval loop reads lock-free**. The fix covered the `DISABLE` paths that mutate the per-instruction tool bytes; it did not touch `active_monitors`, and `_Py_Instrument`'s `LOCK_CODE` at `:1957` was left as-is. The race reported here is the sibling that survived.

## Reproducer

`repro.py` — stdlib only, **exit 66 on 6/6 runs, ~0.6 s each**:

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NW = 4          # worker threads driving generators to close
ROUNDS = 4000
TOOL = 2
EV = sys.monitoring.events

def g():
    yield 1
    yield 2

def cb(*args):
    return None

sys.monitoring.use_tool_id(TOOL, "tsan0036")
for e in (EV.PY_UNWIND, EV.PY_RETURN, EV.PY_RESUME):
    sys.monitoring.register_callback(TOOL, e, cb)

stop = threading.Event()
enter = threading.Barrier(NW + 1)

def worker():
    enter.wait()
    while not stop.is_set():
        for _ in range(50):
            it = g()
            next(it)        # RESUME -> stale version -> _Py_Instrument -> WRITE active_monitors
            it.close()      # gen_close -> _PyEval_NoToolsForUnwind -> READ tools[13]

threads = [threading.Thread(target=worker) for _ in range(NW)]
for t in threads:
    t.start()

enter.wait()
try:
    for r in range(ROUNDS):
        sys.monitoring.set_events(TOOL, EV.PY_UNWIND | EV.PY_RETURN)
        sys.monitoring.set_events(TOOL, EV.PY_RESUME)
        sys.monitoring.set_events(TOOL, 0)
finally:
    stop.set()
    for t in threads:
        t.join()
    sys.monitoring.set_events(TOOL, 0)
    sys.monitoring.free_tool_id(TOOL)
print("done, no crash")
```

The shape mirrors the fleet vehicle (module `pdb`, which drives `sys.monitoring` for exactly this reason). One shared generator code object; several threads create/advance/close generators of it; one thread churns `set_events`, which bumps the global monitoring version and forces every worker's next `RESUME` back through `_Py_Instrument`. `set_events` early-returns when the event set is unchanged (`:2045`), so the churn alternates between three distinct sets.

Run:

```
setarch -R env -u PYTHON_GIL PYTHON_GIL=0 \
  TSAN_OPTIONS='halt_on_error=1:symbolize=1:exitcode=66:history_size=4' \
  DEBUGINFOD_URLS= \
  bash -c 'ulimit -v unlimited; exec .../debug-ft-nojit-tsan/python repro.py'
```

The reproducer's report is byte-for-byte the vehicle's shape: `Write of size 8 at …f28` in `force_instrument_lock_held` vs `Previous read of size 1 at …f2d` in `no_tools_for_local_event` / `_PyEval_NoToolsForUnwind` / `gen_close`, always at word offset +5 (`tools[13]`).

## Severity

**Low.** Honest assessment: this is a real C11 data race (UB), but the practical consequence is bounded.

- The writer's stores are 8-byte aligned and the reader loads a single byte inside one of them. On x86-64 and arm64 that byte is not torn in practice — the reader observes either the pre- or post-update tool mask, not a mixture. So `tools[event]` is **value-benign**: no OOB, no UAF, no refcount damage. `tools[]` is a plain `uint8_t[16]` inside an already-allocated `_PyCoMonitoringData`, and the reader only compares it against 0.
- The visible effect is a **missed or spurious monitoring event** in the window where a tool is being (de)registered. Concretely, in `gen_close` (`Objects/genobject.c:500`):
  ```c
  bool no_unwind_tools = _PyEval_NoToolsForUnwind(_PyThreadState_GET(), frame);
  int oparg = frame->instr_ptr->op.arg;
  if (oparg & RESUME_OPARG_DEPTH1_MASK && no_unwind_tools) {
      FT_ATOMIC_STORE_INT8_RELEASE(gen->gi_frame_state, FRAME_CLEARED);
      gen_clear_frame(gen);
      Py_RETURN_NONE;
  }
  ```
  A stale `no_unwind_tools == true` makes `gen_close` take the shortcut and skip the `PY_UNWIND` callback for that generator; a stale `false` costs a needless full `gen_send_ex2` unwind. Either way the interpreter stays consistent — a debugger/profiler just loses (or gains) one event.
- The `_co_instrumentation_version` release/acquire pairing bounds staleness on weakly-ordered hardware for readers that go through the version check, but as noted this reader doesn't, so on arm64 the stale window is not formally bounded for it.

**How contrived is it?** Moderately, but not artificially: it needs a tool to be registering/unregistering `sys.monitoring` events (or `sys.settrace`) *while* other threads run generators — i.e. a debugger or coverage tool attaching/detaching in a threaded program. That is exactly the workload gh-136870 was filed from (coverage.py under TSan), and the fleet found it through `pdb`, which uses `sys.monitoring` by default since gh-124533. It is not reachable in a steady-state program that sets its monitoring up once and leaves it alone.

The `PY_UNWIND`/`gen_close` pair is simply the face the fuzzer surfaced; the defect is the field, and `no_tools_for_local_event` is called for `RAISE`, `RERAISE`, `PY_THROW`, `STOP_ITERATION`, `EXCEPTION_HANDLED` and `PY_UNWIND` (`Python/ceval.h`, `Python/ceval.c:2445-2466`), so any of those events can present the same race.

## Suggested fix

The reader is on hot exception/unwind paths and cannot take the code object's lock, so the `LOCK_CODE → StopTheWorld` upgrade PR #136994 used is a poor fit for `_Py_Instrument` (which is itself invoked *from* the eval loop, and whose `LOCK_CODE` asserts `!world_stopped`). The cheap, discipline-consistent fix is to make these bytes atomic, matching what the same file already does for the instrumented opcodes:

1. In `no_tools_for_local_event` (`Python/ceval.h`), read via `FT_ATOMIC_LOAD_UINT8_RELAXED(data->active_monitors.tools[event])`. `ceval.h` already includes `pycore_pyatomic_ft_wrappers.h`. Same for `no_tools_for_global_event`'s read of `tstate->interp->monitors.tools[event]`.
2. In `force_instrument_lock_held` (`Python/instrumentation.c:1842`), replace the struct assignment with a per-byte loop of `FT_ATOMIC_STORE_UINT8_RELAXED(code->_co_monitoring->active_monitors.tools[i], active_events.tools[i])` over `_PY_MONITORING_UNGROUPED_EVENTS`. Do the same for the other plain writers of `active_monitors.tools[]` (`:1160`, and the `local_monitors` reads at `:858-859`).

Relaxed ordering is sufficient: the existing `FT_ATOMIC_STORE_UINTPTR_RELEASE` on `_co_instrumentation_version` at `:1926` still provides the publication edge for readers that check the version, and this reader only needs a non-torn, non-UB byte. Both are no-ops on the default (GIL-enabled) build, where `FT_ATOMIC_*` degrade to plain accesses.

A broader alternative — have `no_tools_for_local_event` acquire-load `_co_instrumentation_version` first and fall back to the global monitors when stale — would restore the intended happens-before, but adds a load to the unwind fast path and does not by itself remove the race on `tools[]`.

## Relationship to other catalog entries

**Distinct from TSAN-0030 and TSAN-0029**, on different state, in different functions, with different fixes:

| | shared state | writer | fix |
|---|---|---|---|
| **TSAN-0036** (this) | `code->_co_monitoring->active_monitors.tools[]` — the per-code-object **instrumentation/tools matrix** | `force_instrument_lock_held` (`instrumentation.c:1842`), under `LOCK_CODE` | atomic byte load/store |
| TSAN-0030 | `interp->monitoring_tool_names[]` — the interpreter-global **tool-id registry** | `monitoring_use_tool_id_impl`, unsynchronized | lock/CAS the registry (TOCTOU) |
| TSAN-0029 | `PyFrameObject.f_trace` — **per-frame** legacy trace state | `trace_trampoline` without the frame's critical section | take the frame critical section |

They are all in the `sys.monitoring`/tracing area, and all three are plausibly one umbrella issue's worth of material, but none is a face of another: TSAN-0030 is about *who owns a tool id*, TSAN-0029 is about *a frame's trace function*, and this one is about *which tools are armed on a code object*. Fixing any one leaves the other two.

## Issue search

Searched `python/cpython` for `instrumentation race`, `monitoring race free-threading`, `active_monitors`, `_Py_Instrument`, `no_tools_for_local_event`, `monitoring thread safety`, `sys.monitoring free-threaded`, `instrumentation.c data race`, `gen_close race`. Nearest prior art:

- **gh-136870** *(closed, 2025-07-25)* — "data races in instrumentation when running coverage under TSAN". Same root pattern, same file. Fixed by **PR #136994**, which converted four `LOCK_CODE` sites to stop-the-world for the **bytecode** tool bytes. **Does not cover `active_monitors`**; `_Py_Instrument`'s `LOCK_CODE` is untouched and line 1842 is unchanged since 2023. This finding is the sibling that fix missed — the strongest candidate to reference when filing.
- **gh-131141** *(closed)* — "Data race between `_PyMonitoring_RegisterCallback` and `_Py_call_instrumentation_2args`". Different field (`interp->monitoring_callables`), already fixed.
- **gh-135633** *(open)* — "Potential thread unsafety in `test_free_threading.test_monitoring`". Unresolved flakiness (assertion failures, refleaks, 45-min timeouts) in the `sys.settrace` monitoring tests, root cause never found. Plausibly related territory; not the same identified defect, but worth mentioning as a symptom this could contribute to.
- **gh-116775 / gh-116818** *(closed)* — "Make `sys.settrace`, `sys.setprofile`, and monitoring thread-safe". The original FT-hardening pass for this subsystem; predates the `active_monitors` gap.

**Verdict: NEW** — no open or closed issue identifies this read site. In the remit of **gh-116738** ("Audit all built-in modules for thread safety") and a natural addition to **#149816** ("22 free-threading race conditions"), but most precisely framed as unfinished business from **gh-136870**.
