# Data race: shared `decimal.Context` — `clear_flags()` writes `ctx->status` while `repr()`/`.flags` reads it (`_decimal.c:1421` vs `:1570`)

*A `decimal.Context` embeds an `mpd_context_t` whose `status` field (`uint32_t`) is read and written with plain, unsynchronized accesses. `ctx.clear_flags()` stores `ctx->status = 0` while a concurrent `repr(ctx)` reads `ctx->status` to render the flag list — a TSan data race on a shared Context. This is the same non-atomic `mpd_context_t::status` field already reported upstream as [cpython#149142](https://github.com/python/cpython/issues/149142); this is a second call-site pair (`clear_flags` vs `repr`) of that same bug.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Modules/_decimal/_decimal.c` stores each `Context`'s state in an `mpd_context_t ctx` embedded directly in `PyDecContextObject` (`_decimal.c:221`). The `status` field is a `uint32_t` (the accumulated condition flags). Context methods touch it with ordinary C loads/stores and **no** lock, critical section, or atomic — `_decimal.c` contains zero `Py_BEGIN_CRITICAL_SECTION`/`_Py_atomic`/`FT_ATOMIC` uses.

The write side (`clear_flags`):

```c
static PyObject *
_decimal_Context_clear_flags_impl(PyObject *self)
{
    CTX(self)->status = 0;   /* :1421  write (uint32_t) */
    Py_RETURN_NONE;
}
```

The read side (`repr`):

```c
static PyObject *
context_repr(PyObject *self)
{
    ...
    ctx = CTX(self);
    ...
    n = mpd_lsnprint_signals(flags, mem, ctx->status, dec_signal_string);  /* :1570  read */
    ...
}
```

When one thread calls `ctx.clear_flags()` and another calls `repr(ctx)` (or reads `ctx.flags`, which reads the same field via `signals_as_list`, `:1722`) on the **same shared Context**, TSan reports a data race on `ctx->status`. `repr()` and `.flags` look read-only to callers, so a shared `Context` is not safe to inspect while another thread mutates its flags.

## Reproducer

```python
import sys, threading
from decimal import Context
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A single decimal.Context shared across threads. Its C struct embeds an
# mpd_context_t whose `status` field (uint32_t) is read/written with plain,
# unsynchronized accesses:
#   ctx.clear_flags()  -> _decimal_Context_clear_flags_impl: CTX(self)->status = 0   (write, _decimal.c:1421)
#   repr(ctx)          -> context_repr:  mpd_lsnprint_signals(..., ctx->status, ...) (read,  _decimal.c:1570)
# Concurrent writers (clear_flags) and readers (repr) race on ctx->status.
NT = 8
ITERS = 200_000
ctx = Context()
barrier = threading.Barrier(NT)

def writer():
    barrier.wait()
    for _ in range(ITERS):
        ctx.clear_flags()          # write ctx->status = 0

def reader():
    barrier.wait()
    for _ in range(ITERS):
        repr(ctx)                  # read ctx->status (renders the flags)

ts = [threading.Thread(target=(writer if i % 2 == 0 else reader)) for i in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

Fires within a fraction of a second (well under the barrier's first batch); deterministic exit 66.

## TSan report (confirmed, CPython 3.16.0a0 `heads/main:bcf98ddbc40`, `--disable-gil --with-thread-sanitizer`, Clang 21)

```
WARNING: ThreadSanitizer: data race (pid=2149719)
  Write of size 4 at 0x7fffb633c66c by thread T1:
    #0 _decimal_Context_clear_flags_impl Modules/_decimal/_decimal.c:1421:23   (CTX(self)->status = 0)
    #1 _decimal_Context_clear_flags      Modules/_decimal/clinic/_decimal.c.h:204:12
    #2 cfunction_vectorcall_NOARGS       Objects/methodobject.c:508
    ...
    #29 thread_run                       Modules/_threadmodule.c:388

  Previous read of size 4 at 0x7fffb633c66c by thread T8:
    #0 context_repr                      Modules/_decimal/_decimal.c:1570:47   (mpd_lsnprint_signals(..., ctx->status, ...))
    #1 PyObject_Repr                     Objects/object.c:784
    #2 builtin_repr                      Python/bltinmodule.c:2677
    ...
    #26 thread_run                       Modules/_threadmodule.c:388

SUMMARY: ThreadSanitizer: data race Modules/_decimal/_decimal.c:1421:23 in _decimal_Context_clear_flags_impl
```

Exit code 66. The confirmed signature matches the fleet-seeded one exactly (same two functions and lines: write `_decimal_Context_clear_flags_impl:1421`, read `context_repr:1570`).

## Root cause

`mpd_context_t` (libmpdec `mpdecimal.h`):

```c
typedef struct mpd_context_t {
    mpd_ssize_t prec, emax, emin;
    uint32_t traps;     /* trapped conditions */
    uint32_t status;    /* accumulated condition flags   <-- racing field */
    uint32_t newtrap;
    int round, clamp, allcr;
} mpd_context_t;
```

The struct is embedded by value in the Python object (`PyDecContextObject.ctx`, `_decimal.c:221`), so `ctx->status` lives inside the shared object. Every accessor uses a plain access with no synchronization:

- **write to 0:** `clear_flags` (`:1421`), plus the reset paths `:1825/:1901/:1924/:1985`.
- **read:** `context_repr` (`:1570`) and the `.flags` property via `signals_as_list` (`:1722`).
- **read-modify-write:** arithmetic that raises conditions does `ctx->status |= status` (`:616`, and `:3498/:3513` for float operations) — a non-atomic RMW, so two threads doing arithmetic in a shared context can additionally *lose* flag updates.

Under free-threading there is no GIL serializing these, and `_decimal.c` adds no `Py_BEGIN_CRITICAL_SECTION` or atomics, so concurrent access to `ctx->status` on a shared `Context` is a genuine data race. The 4-byte field is naturally aligned, so an individual load/store does not tear; the observable consequences are limited to a stale/garbled flag set in the rendered `repr`/`.flags` result or a lost flag update from a racing `|=`. No memory-safety violation (no pointer/refcount involved), so it does not crash.

## Relationship to cpython#149142 (duplicate)

This is **the same underlying bug** as [cpython#149142](https://github.com/python/cpython/issues/149142) — *"_decimal: `mpd_context_t::status/traps` mutated non-atomically leading to data race"* (filed by devdanzin, OPEN; PR [#150598](https://github.com/python/cpython/pull/150598)). That issue is precisely about non-atomic access to the `status`/`traps` fields of a shared Context. `clear_flags`-vs-`repr` is just one concrete call-site pair of that class; the fix contemplated by #149142/#150598 (make `status`/`traps` access atomic, or guard Context field access with a per-object critical section) resolves this pair too.

Confirmed still live on `heads/main:bcf98ddbc40` (Jul 2026) — `_decimal.c` currently has no atomics/critical sections, so PR #150598 has not landed on this build. Reported here as a **duplicate of #149142** rather than a new bug; kept in the catalog as a distinct reproduced vehicle/signature for that issue.

(Distinct from TSAN-0005, which is the separate cached-hash race on a `Decimal` object's `hash` field — a different type and a different field.)

## Impact / severity

**Low.** A real, TSan-confirmed data race, but crash-free: the racing field is a naturally-aligned `uint32_t` used only for condition flags, with no pointer/refcount/lifetime consequences. Worst observable effects are a momentarily inconsistent flag set in a `repr()`/`ctx.flags` read, or a lost flag bit when arithmetic's `|=` races another writer. It surfaces only when a single `Context` object is deliberately shared and mutated across threads (arguably "don't share a Context across threads"), but because `repr()`/`.flags` are read-only-looking, and because a Context can legitimately be shared, it is worth fixing for free-threading correctness — which is exactly what #149142 tracks.

## Suggested fix

Same as #149142/#150598: make the `status` (and `traps`) accesses well-defined for free-threading. Either

- route every read/write/RMW of `ctx->status` and `ctx->traps` through relaxed atomics (`FT_ATOMIC_LOAD_UINT32_RELAXED` / `FT_ATOMIC_STORE_UINT32_RELAXED`, and an atomic `fetch_or` for the `|=` sites), or
- guard the context field accessors (`clear_flags`, `context_repr`, the `flags`/`traps` properties, and the arithmetic status-raise path) with `Py_BEGIN_CRITICAL_SECTION(self)` so all touches of a given Context serialize.

Atomics are the lighter-weight option for these scalar fields and match the intent of PR #150598.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet `fusil-tsan_fleet_02`, vehicle `inst-01/python/_decimal-warning_threadsanitizer_data_race-tsanNEW` (the fuzzer exercised `getcontext`/`localcontext`/`setcontext`/`IEEEContext` concurrently). Minimal reproducer uses only `decimal` + `threading`.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. This one is a duplicate of the already-filed cpython#149142 and is recorded for cross-reference, not for separate filing.*
