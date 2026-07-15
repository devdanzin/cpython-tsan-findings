# NOPARSE crashes that are the harness aborting itself (not TSan findings)

**Short version:** a `--tsan` fleet's `tsanNOPARSE` bucket (a crash the deduper kept but could not
parse a `WARNING: ThreadSanitizer: data race` out of) can contain the target process **destroying
itself** by calling a process-lifecycle / self-signalling builtin from the stress region. These are
**not** CPython or TSan bugs — the fuzzer told the interpreter to abort/fork. fusil PR #205
(`TSAN_UNSAFE_CALLS`) removed the cause by excluding those builtins from the shared objects'
callable surface; this note is for triagers looking at pre-#205 crash dirs or archived fleet data.

## Why it happened

The stress region shares the *target module object itself* and, per worker, calls
`getattr(module, name)()` for every public `name` in `dir(module)`. Before #205 that list was
unfiltered, so for a module like `posix`/`os`/`pty` it included builtins that never return normally:

| NOPARSE label | culprit call | mechanism | terminal output |
|---|---|---|---|
| `posix-sigabrt` | `posix.abort()` (≡ `os.abort()`) | C `abort()` raises **SIGABRT** | *nothing* after `[TSAN] entering concurrency-stress region` — clean signal 6, no TSan banner |
| `pty-bug` | `pty.fork()` / `os.forkpty()` | fork() in a multithreaded TSan process → child's TSan `ThreadState` is broken → SEGV inside `__tsan::TraceSwitchPart` | `ThreadSanitizer:DEADLYSIGNAL` + `nested bug in the same thread, aborting.` |

Both faces reduce to one root cause: **a worker thread invoked a self-destruct builtin on the
shared module object.** Neither is a data race; neither is reachable after #205.

## Distinguishing a self-abort from a *real* fatal error

A SIGABRT (signal 6) under `--tsan` is **not** automatically noise — keep the NOPARSE bucket manual:

- **Real TSan finding** → exits `66` (our `TSAN_OPTIONS` `exitcode=66`), with a
  `WARNING: ThreadSanitizer: data race` banner. Parses fine; never lands in NOPARSE.
- **TSan runtime artifact** (fork-in-thread, the `pty` face) → SIGABRT/SEGV **with** a
  `ThreadSanitizer:DEADLYSIGNAL` / `CHECK failed` / `nested bug ... aborting` banner. Noise, but the
  banner tells you so.
- **Harness self-abort** (the `posix` face) → SIGABRT with **no** ThreadSanitizer text whatsoever,
  and stdout ending exactly at `[TSAN] entering concurrency-stress region`. Noise.
- **A genuine CPython fatal error** (e.g. a debug-build `assert()` / `Py_FatalError` tripped under
  concurrency) → SIGABRT with a `Fatal Python error:` / `Assertion ... failed` banner. **This would
  be interesting** — which is exactly why we do *not* blanket-suppress signal-6/NOPARSE in the
  deduper: a bare-abort auto-drop would hide this case.

So the triage rule is: a signal-6 NOPARSE with **no** message at all is a harness self-abort
(pre-#205 `os.abort`); one carrying a `Fatal Python error:` / `Assertion` banner is a real crash to
chase.

## Reproducer (the `posix-sigabrt` face)

```sh
DEBUGINFOD_URLS= setarch -R env PYTHON_GIL=0 \
  TSAN_OPTIONS="halt_on_error=1 symbolize=1 exitcode=66 history_size=4" \
  ./python -c 'import posix; posix.abort()'
# -> "Aborted", exit 134 (128+6), no ThreadSanitizer output. Matches the crash dir exactly.
```

## Disposition

Non-bug, fixed at source by fusil PR #205 (`TSAN_UNSAFE_CALLS` excludes
`fork`/`forkpty`/`spawn*`/`exec*`/`_exit`/`abort`/`system`/`popen`/`register_at_fork` from both the
curated `_tsan_funcs` list and the runtime `dir()` loop). No catalog signature, no suppression entry
(there is no TSan report to match on), and intentionally **no** deduper auto-drop of signal-6 — the
NOPARSE bucket stays a manual glance so a real `Fatal Python error` can't hide in it.
