# Data race: `socket.gettimeout()` reads `sock_timeout` non-atomically while `socket.setblocking()` writes it (`socketmodule.c:3308` vs `:3172`)

TSAN-0035 · found by `fusil --tsan` (fleet-04, `ssl` module vehicle) · CPython 3.16.0a0 free-threaded

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`PySocketSockObject.sock_timeout` (`Modules/socketmodule.h:347`, a `PyTime_t` = `int64_t`) is
per-socket mutable state that Python-level methods both read and write with **plain, non-atomic
accesses**. Calling `s.gettimeout()` on a socket while another thread calls `s.setblocking()` on it
is a data race on that field:

| | site | access |
|---|---|---|
| write | `sock_setblocking` — `socketmodule.c:3172:21` | `s->sock_timeout = _PyTime_FromSeconds(block ? -1 : 0);` |
| read | `sock_gettimeout_impl` — `socketmodule.c:3308:12` | `if (s->sock_timeout < 0) {` |

Neither takes a critical section; neither uses an atomic. **All 15 accesses to `sock_timeout` in
`socketmodule.c` are plain** — there is no synchronization on this field anywhere.

This is an **incomplete free-threading conversion**, not an isolated oversight. Two neighbouring
pieces of state were explicitly converted, and `sock_timeout` was left behind:

- `s->sock_fd` — the sibling field on the **same struct** — was given relaxed-atomic accessors
  `get_sock_fd`/`set_sock_fd` (`socketmodule.c:567-592`) by **gh-128277 / PR #128304**.
- `state->defaulttimeout` — a `PyTime_t`, the **exact same type** as `sock_timeout` — was made
  atomic by **gh-116616 / PR #116623**.

The primitive the fix needs is therefore already used in this very file, on this very type:
`socketmodule.c:1134` reads the default with `_Py_atomic_load_int64_relaxed(&state->defaulttimeout)`
— and assigns the result into `s->sock_timeout` with a plain store.

## Reproducer

`repro.py` — stdlib only, no network (an unconnected socket has a `sock_timeout` like any other).
One writer thread + 4 reader threads on one shared socket, so the only pair TSan can report is the
read/write pair of interest.

Run under the standard wrapper:

```
setarch -R env -u PYTHON_GIL PYTHON_GIL=0 \
  TSAN_OPTIONS='halt_on_error=1:symbolize=1:exitcode=66:history_size=4' \
  DEBUGINFOD_URLS= \
  bash -c 'ulimit -v unlimited; exec .../builds/debug-ft-nojit-tsan/python repro.py'
```

**Hit rate: 10/10 runs exit 66**, typically within a second. The reported sites and `.so` offsets
match the fleet vehicle exactly (`_socket...so+0x10f46` for `sock_setblocking`).

## TSan report

See `tsan_report.txt` for the full block. The two stanzas:

```
WARNING: ThreadSanitizer: data race (pid=3522357)
  Read of size 8 at 0x7fffb68d0238 by thread T4:
    #0 sock_gettimeout_impl ./Modules/socketmodule.c:3308:12
    #1 sock_gettimeout_method ./Modules/socketmodule.c:3320:12
    #2 method_vectorcall_NOARGS Objects/descrobject.c:448:24
  Previous write of size 8 at 0x7fffb68d0238 by thread T5:
    #0 sock_setblocking ./Modules/socketmodule.c:3172:21
    #1 cfunction_vectorcall_O Objects/methodobject.c:536:24
SUMMARY: ThreadSanitizer: data race ./Modules/socketmodule.c:3308:12 in sock_gettimeout_impl
```

## Root cause

`sock_setblocking` (`socketmodule.c:3165-3177`):

```c
static PyObject *
sock_setblocking(PyObject *self, PyObject *arg)
{
    long block;

    block = PyObject_IsTrue(arg);
    if (block < 0)
        return NULL;

   PySocketSockObject *s = _PySocketSockObject_CAST(self);
    s->sock_timeout = _PyTime_FromSeconds(block ? -1 : 0);   /* :3172 — plain store */
    if (internal_setblocking(s, block) == -1) {              /* :3173 */
        return NULL;
    }
    Py_RETURN_NONE;
}
```

`sock_gettimeout_impl` (`socketmodule.c:3305-3315`):

```c
static PyObject *
sock_gettimeout_impl(PyObject *self, void *Py_UNUSED(ignored))
{
    PySocketSockObject *s = _PySocketSockObject_CAST(self);
    if (s->sock_timeout < 0) {                               /* :3308 — plain load */
        Py_RETURN_NONE;
    }
    else {
        double seconds = PyTime_AsSecondsDouble(s->sock_timeout);  /* :3312 */
        return PyFloat_FromDouble(seconds);
    }
}
```

`sock_settimeout` (`:3264`) has the identical shape — plain store, then `internal_setblocking`.
`sock_getblocking` (`:3194`) is a third plain reader.

## Impact / severity

**Low.** This is a real data race, but as compiled it is a **benign value race**, and I could not
demonstrate any Python-visible misbehaviour. Being specific about what was checked and what was
ruled out:

**No tearing.** `sock_timeout` is a naturally-aligned 8-byte scalar, so the load and store are
each atomic in hardware on x86-64. A reader observes either the old or the new value — never a
mix. Both `setblocking(False)` (→ `0`) and `setblocking(True)` (→ `-1s`) are legal values, so a
stale read just means `gettimeout()` returns the answer from a moment earlier — inherent to
racing the call anyway.

**The obvious TOCTOU is not real — I checked the machine code.** `sock_gettimeout_impl` reads
`s->sock_timeout` twice in the C source (`:3308` compare, `:3312` argument). If those were two
separate loads, an interleaved `setblocking(True)` could make the compare see `0` and the reload
see `-1s`, so `gettimeout()` would return **`-1.0`** — out of contract (documented as `None` or a
non-negative float). **The compiler coalesces both source reads into one load**, so this cannot
happen here:

```
socketmodule.c:3308
  111bd:  call   __tsan_read8@plt     # one instrumented read
  111c2:  mov    0x38(%rbx),%rdi      # ONE load; %rdi is both the compared value ...
  111c6:  test   %rdi,%rdi
  111c9:  js     111da                # ... and
socketmodule.c:3312
  111cb:  call   PyTime_AsSecondsDouble@plt   # ... the argument, unreloaded
```

The same holds for the other double-read, `_socket_socket_sendall` (`:4691` `has_timeout` /
`:4692` `timeout`): one `__tsan_read8`, one `mov` into `%r14`. So `has_timeout` and `timeout`
cannot disagree either. An empirical probe agreed — 0 contract violations in ~10k racing
`gettimeout()` calls.

This coalescing is a **compiler courtesy, not a guarantee**: the C11 model makes the race UB, and
a compiler is entitled to reload. The TOCTOU is latent, not live.

**A compound-state hazard exists but I could not trigger it.** `setblocking` updates two things
non-atomically: the `sock_timeout` field (`:3172`), then the FD's `O_NONBLOCK` flag via
`internal_setblocking` (`:3173`). Two threads calling `setblocking(True)`/`setblocking(False)`
could in principle interleave `store_a, store_b, ioctl_b, ioctl_a`, leaving `sock_timeout == 0`
("non-blocking") while the FD is genuinely **blocking**. That state would matter: `sock_call_ex`
gates its `select()` on `has_timeout = (timeout > 0)` (`:971`), so a `sock_timeout` of `0` skips
`select()` and calls `recv()` straight on a blocking FD — **a hang**. But the window between the
store and the `ioctl` is ~2 instructions, and I observed **0 divergences in 28,800 racing pairs**
(oversubscribed 96 threads on 16 cores, checking `getblocking()` against the real
`fcntl(F_GETFL) & O_NONBLOCK`). So this is a theoretical hazard, reported as such — not a
demonstrated bug.

Worth fixing because it is cheap, it is squarely in the gh-116738 remit, and it removes UB that
today only happens to be harmless. Not worth fixing urgently.

## Real bug vs. expected

**Real, in scope.** A socket shared across threads is an entirely ordinary pattern, and
`Modules/socketmodule.c` was explicitly hardened for free-threading (gh-128277); the fix is atomics
or a critical section matching whatever the writer uses. Here the module has already made that choice
twice on this very struct (`sock_fd`, `state->defaulttimeout`) and simply missed this field.

## Suggested fix

Mirror the existing `get_sock_fd`/`set_sock_fd` pattern with a `get_sock_timeout`/`set_sock_timeout`
accessor pair over `_Py_atomic_load_int64_relaxed` / `_Py_atomic_store_int64_relaxed` (already used
on a `PyTime_t` at `socketmodule.c:1134`), and route all 15 accesses through them. Relaxed ordering
suffices — it matches what `defaulttimeout` already does. The non-free-threaded build is
unaffected (the FT wrappers are plain accesses there).

```c
static inline void
set_sock_timeout(PySocketSockObject *s, PyTime_t timeout)
{
    _Py_atomic_store_int64_relaxed(&s->sock_timeout, timeout);
}

static inline PyTime_t
get_sock_timeout(PySocketSockObject *s)
{
    return _Py_atomic_load_int64_relaxed(&s->sock_timeout);
}
```

Note this silences TSan and removes the UB but does **not** address the compound-state hazard
above — `setblocking`/`settimeout` would still update the field and the FD flag non-atomically.
Closing that too requires a per-socket critical section around the whole method. Given it could
not be triggered in 28,800 attempts, the atomics alone are a reasonable first step; the critical
section is a judgement call for the maintainers.

## Issue search — verdict: NEW

Searched `gh search issues/prs --repo python/cpython` for `socketmodule`, `sock_timeout`,
`socket free-threading`, `socket data race`, `_socket thread`, `settimeout race`,
`socketmodule thread safety`, `socketmodule critical section`. Nothing covers this field.

- **gh-116738** ("Audit all built-in modules for thread safety") — **in remit, and already
  checked off**: the list contains `- [x] Modules/socketmodule.c`. This finding is evidence that
  checkbox is premature.
- **gh-128277** ("Make `socket` module thread safe") — **CLOSED**, opened precisely because of
  TSan warnings from the socket module. Its three merged PRs are #128286 (globals), #128304
  (`sock_fd` atomics) and #128305 (remove a critical section from `socket.close`). **None of the
  three diffs touches `sock_timeout`** (verified: `gh pr diff | grep -c sock_timeout` = 0 for all
  three). This is the direct predecessor and the gap it left.
- **gh-116616** ("use atomics to access `defaulttimeout`") — CLOSED; converted the module-global
  `PyTime_t` timeout. Its per-socket twin was never done. Does not mention `sock_timeout`.
- **#149816** ("22 free-threading race conditions") — **does not list `_socket`** at all; no
  overlap.
- **gh-74667** (`getservbyname` et al. not threadsafe) — different subject (global netdb calls).

So: not filed. It belongs under gh-128277's story and should reference it.

## Notes

Vehicle: `fusil-tsan_fleet_04/inst-03/python-2/ssl-warning_threadsanitizer_data_race-tsanNEW`
(surfaced via the `ssl` module, which reads `sock->sock_timeout` directly in four places —
`_ssl.c:453`, `:839`, `:1024`, `:2543` — all plain, so `_ssl` inherits the same race).
