# Umbrella issue draft — free-threading data races (fusil --tsan)

Draft for a CPython umbrella issue. Gists are published (URLs below); once the umbrella issue is
filed, each gist gets a trailing note pointing back at it (see `scripts/` / the OOM catalog's
`gen_index`-style backlink step). Post the umbrella yourself so the issue template is followed.

---

## Title

`Free-threading data races in CPython: 15 findings (9 new + 6 residual of existing FT work)`

## Body

### What happened?

ThreadSanitizer fuzzing of free-threaded CPython `main` turned up **15 data races** where a built-in object or interpreter-global state is accessed concurrently without the atomics or critical section the surrounding code (or the documentation) already assumes. Each has a minimal, stdlib-only reproducer, the raw TSan report, a root-cause analysis against current-`main` source, and a suggested fix, published as a self-contained gist (linked below).

They split into two groups: **9 are new defects**, and **6 are residuals of free-threading work that is already merged or documented** — a specific reader/field/path that a completed conversion left behind. The residuals are the lowest-risk to fix (the pattern and the fix are already in the tree next to them); the related issue/PR is named in the table.

I'm filing them under one umbrella so they can be picked off individually rather than flooding the tracker. **To take one:** open a normal CPython issue or PR and drop a comment here with the link — I'll mark it in the table. If any is a duplicate or a non-bug, say so and I'll annotate it.

Found with [fusil](https://github.com/devdanzin/fusil)'s `--tsan` mode (fusil originally by @vstinner). Reports and reproducers were drafted with AI assistance (Claude Code) and then reviewed and re-verified by hand — see *Disclosure*.

### Reproducing

- Found on `main` (3.16.0a0) on a `--disable-gil --with-thread-sanitizer` debug build. These are concurrency bugs, not tied to an exact revision.
- Each gist ships a minimal stdlib-only `TSAN-NNNN-repro.py`. Run it on a free-threaded TSan build with `PYTHON_GIL=0`; a real race exits non-zero and prints `WARNING: ThreadSanitizer: data race`. (TSan needs ASLR reduced — e.g. `setarch -R` — and an unlimited `RLIMIT_AS`, or it runs degraded.)
- Most are **value-benign on aligned hardware** but are genuine C-level data races (formally UB, and TSan-reported); a few carry a latent UAF/leak/crash, called out per report. **All 15 reproduce in isolation** with a stdlib-only script (some probabilistically — loop them).

### New free-threading defects (9)

Status legend: blank = not yet filed · `#N` = open issue/PR · `#N FIXED` = filed and fixed.

| Report | Race | Suggested fix | Status |
|---|---|---|---|
| [TSAN-0001](https://gist.github.com/devdanzin/ed0c939eb845d23ca67b464d3e53ff56) | `multibytecodec.c`: `MultibyteIncrementalDecoder.getstate()`/`reset()`/`decode()` on a shared decoder race its `pending`/`pendingsize`/`state` fields — the incremental codecs have no critical sections (incl. the iso-2022/HZ face) | per-object critical sections on the incremental codec methods | |
| [TSAN-0005](https://gist.github.com/devdanzin/4ece3c7d20810f1ad33e2b204ccf33e4) | `_decimal.c`: `Decimal.__hash__` writes its lazy hash cache (`self->hash`) with plain stores; concurrent `hash()` of a shared `Decimal` races | relaxed atomics on `self->hash` | |
| [TSAN-0006](https://gist.github.com/devdanzin/db21f2b29ab7572ce6c111b57b3cea5c) | `itertoolsmodule.c`: `count.__repr__` plain-reads `cnt` while `count_next` writes it with an **atomic CAS** — the writer was hardened, the reader missed | `_Py_atomic_load_ssize_relaxed` in `count_repr` | #153908 FIXED |
| [TSAN-0011](https://gist.github.com/devdanzin/0b13838fd6089e73a3f063ed8f68e733) | `sysmodule.c`: `sys.addaudithook` lazily creates `interp->audit_hooks` with **no lock**, racing `should_audit` on every audit event — **security-relevant** (PEP 578); the sibling C-level hook list is already mutex-guarded | serialize under `runtime->audit_hooks.mutex`; atomics on the pointer | |
| [TSAN-0018](https://gist.github.com/devdanzin/6b99ef6dce08ac64d8d1d379308a8f86) | `dictobject.c`: readers of a shared dict's `dk_nentries` use plain loads while `setattr`/insert bump it atomically — including the **public `PyDict_Next` C-API** (via `_PyType_GetSubclasses`), so any extension iterating a shared dict is exposed. `LOAD_KEYS_NENTRIES` already exists at `:237` | `LOAD_KEYS_NENTRIES` at the reader sites (`_PyObject_IsInstanceDictEmpty`, `clear_lock_held`, `_PyDict_Next`) | #153881 |
| [TSAN-0030](https://gist.github.com/devdanzin/e6e667ea59d98e8a3761e3915bc58ff9) | `instrumentation.c`: `sys.monitoring.use_tool_id()` is an unsynchronized check-then-act on the interpreter-global `monitoring_tool_names[]` — both threads pass the guard → leak + dup ownership; `free_tool_id`'s `Py_CLEAR` is a UAF/double-free. All four accessors are unlocked | lock/atomic the tool-id registry; fix all four accessors together | |
| [TSAN-0031](https://gist.github.com/devdanzin/a24f57a318cf6974caa5b04d134d8fbd) | `_elementtree.c`: concurrent feed of one shared `TreeBuilder` races its parse state (`this`/`last`/`data`/`index`/`stack`) — the module has **zero** critical sections, yet declares `Py_MOD_GIL_NOT_USED` | `@critical_section` the whole `TreeBuilder` feed path (`start`/`data`/`end`/`comment`/`pi`/`close`) | (abandoned PR gh-145569 covered only `handle_end`) |
| [TSAN-0035](https://gist.github.com/devdanzin/1ee2570ab23f267edce7236d0877a632) | `socketmodule.c`: `sock_timeout` is read/written with plain accesses (`gettimeout()` vs `setblocking()`) — the one per-socket scalar the module's free-threading conversion (gh-128277) missed, while the sibling `sock_fd` and `state->defaulttimeout` were made atomic. `_ssl.c` inherits it | `get/set_sock_timeout` over `_Py_atomic_{load,store}_int64_relaxed`, mirroring `sock_fd` | #153935 |
| [TSAN-0036](https://gist.github.com/devdanzin/91b26da5b484234d0ad93027945bcdda) | `instrumentation.c`/`ceval.c`: the eval loop reads a code object's `active_monitors.tools[]` **lock-free** (`no_tools_for_local_event`, via `gen_close`) while `_Py_Instrument` replaces the struct under `LOCK_CODE` — which the eval loop never takes; lazy re-instrumentation runs with the world running | relaxed atomics on the `tools[]` bytes (matching the file's opcode discipline) | (PR gh-136994 did exactly this for the bytecode tool bytes but not `active_monitors`) |

### Residuals of existing / documented free-threading work (6)

These are a reader/field/path left behind by a completed (or documented) conversion — likely best handled as a follow-up to the named issue rather than as fresh bugs.

| Report | Race | Related upstream | Status |
|---|---|---|---|
| [TSAN-0002](https://gist.github.com/devdanzin/0c3bea3347a169cb64f40873a6dcc3bd) | `_zstd`: `ZstdCompressor.last_mode` is stored plain but read lock-free via its `Py_T_INT` member descriptor | residual of gh-133885 / gh-134289 (added locks, left `last_mode` plain) | |
| [TSAN-0013](https://gist.github.com/devdanzin/6bd1bd3936235547e9c0abd8eb3cca18) | shared `list`: non-atomic readers (`Py_SIZE`/unpack, `stringlib_bytes_join`, `marshal`) race `list_resize`'s atomic `ob_item`/`ob_size` publish | reader-side residual of gh-129069; contract documented in gh-142519 | |
| [TSAN-0014](https://gist.github.com/devdanzin/c1a716a8ad7dff56554f291376eaef66) | shared `list`: `list.sort()`'s in-place `binarysort` rewrite (no critical section) races a concurrent lock-free reader | sort-path residual of the gh-129069 / gh-142519 list class | |
| [TSAN-0025](https://gist.github.com/devdanzin/f0b4f8859da46e985f23aa9cadaaa4c9) | `readline.c`: `set_auto_history()` writes the module-global `should_auto_add_history` (a plain `static int`) unsynchronized | belongs with the readline FT cleanup (gh-153291) | |
| [TSAN-0029](https://gist.github.com/devdanzin/987cdee793b09c28d9a337c6f91647a7) | `frameobject.c`/`sysmodule.c`: `trace_trampoline` writes a running frame's `f_trace` with no critical section while the `f_trace`/`f_trace_opcodes` accessors are `@critical_section` | the legacy `settrace` path not brought under the recent frame-accessor FT hardening; gh-116738 remit | (low; needs mutating another thread's live frame) |
| [TSAN-0034](https://gist.github.com/devdanzin/1e787ea3420c990f4c7728048184e6a4) | finalization: `handle_thread_shutdown_exception` reads `interp->threads.head` in an `assert()` **before** `_PyEval_StopTheWorld`, racing a concurrent `HEAD_LOCK`-held write of it (`add_threadstate` on create / `tstate_delete_common` on exit) | — | (debug-only: the read is inside the assert; reproduced in isolation, ~44 %/run) |

### Disclosure

These findings were produced with AI assistance: fusil's `--tsan` mode generated the concurrency stress that surfaced them, and Claude Code drafted the reports and reduced the reproducers. Every reproducer was then run and re-verified by hand on the free-threaded TSan build, and every root cause was checked against current-`main` source. Where a finding is not reproduced in isolation, it says so.

---

## Not in the umbrella (for our own tracking)

- **Already filed:** TSAN-0007 (#153296), TSAN-0010 (#151370), TSAN-0012 (#151363), TSAN-0015 (#151627), TSAN-0016 (#153291), TSAN-0019 (#149142), TSAN-0023 (#149816), TSAN-0024 (#151707),
  TSAN-0027 (#151377), TSAN-0028 (#153292, fixed), TSAN-0032 (#149816), **TSAN-0033 (#153809** — the `_asyncio.Task` refcount-0-while-tracked crash, filed standalone as it hangs/crashes release builds).
- **Not a bug / out of scope:** TSAN-0003 (glibc/TSan FP), TSAN-0020 (OpenSSL libcrypto), TSAN-0008 (residual of gh-116738/#138229 — a note at most), TSAN-0009 (bundled single-threaded libexpat, don't-share-the-parser).
- **Folded:** TSAN-0004→0001, TSAN-0010(face)→0013, TSAN-0017→0002, TSAN-0021/0022→0018/0013/0009.

## Gist URLs (for the post-filing backlink pass)

Once the umbrella is filed as `#NNNNN`, append to each gist a line like
"Tracked in the umbrella issue python/cpython#NNNNN" (and flip any that get their own issue).

```
TSAN-0001  https://gist.github.com/devdanzin/ed0c939eb845d23ca67b464d3e53ff56
TSAN-0002  https://gist.github.com/devdanzin/0c3bea3347a169cb64f40873a6dcc3bd
TSAN-0005  https://gist.github.com/devdanzin/4ece3c7d20810f1ad33e2b204ccf33e4
TSAN-0006  https://gist.github.com/devdanzin/db21f2b29ab7572ce6c111b57b3cea5c
TSAN-0011  https://gist.github.com/devdanzin/0b13838fd6089e73a3f063ed8f68e733
TSAN-0013  https://gist.github.com/devdanzin/6bd1bd3936235547e9c0abd8eb3cca18
TSAN-0014  https://gist.github.com/devdanzin/c1a716a8ad7dff56554f291376eaef66
TSAN-0018  https://gist.github.com/devdanzin/6b99ef6dce08ac64d8d1d379308a8f86
TSAN-0025  https://gist.github.com/devdanzin/f0b4f8859da46e985f23aa9cadaaa4c9
TSAN-0029  https://gist.github.com/devdanzin/987cdee793b09c28d9a337c6f91647a7
TSAN-0030  https://gist.github.com/devdanzin/e6e667ea59d98e8a3761e3915bc58ff9
TSAN-0031  https://gist.github.com/devdanzin/a24f57a318cf6974caa5b04d134d8fbd
TSAN-0034  https://gist.github.com/devdanzin/1e787ea3420c990f4c7728048184e6a4
TSAN-0035  https://gist.github.com/devdanzin/1ee2570ab23f267edce7236d0877a632
TSAN-0036  https://gist.github.com/devdanzin/91b26da5b484234d0ad93027945bcdda
```
