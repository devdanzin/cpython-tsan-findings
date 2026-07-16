# Data race: `buffered_iternext` reads `self->ok` via `CHECK_INITIALIZED` outside its critical section, racing `BufferedReader.detach()`'s `self->ok = 0` write (`bufferedio.c:1504` vs `:628`)

*`io.BufferedReader.detach()` (`_io__Buffered_detach_impl`) is correctly decorated `@critical_section`, so its `self->raw = NULL; self->detached = 1; self->ok = 0;` writes run under the per-object critical section. But `buffered_iternext` — the `tp_iternext` slot reached by `next(reader)` / `for line in reader` / `list(reader)` — does its `CHECK_INITIALIZED(self)` (which reads `self->ok`) at the very top of the function, **before** it opens its own critical section around `_buffered_readline`. That first flag read is unprotected and races `detach()`'s store. This is a residual of the already-merged fix PR #150295 (gh-149816 item #84), which added the critical section around `_buffered_readline` but left the leading `CHECK_INITIALIZED` outside it.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

On the free-threaded build, `io.BufferedReader` / `io.BufferedRandom` methods are made memory-safe by taking the object's critical section. `detach()` is one such method — its Argument Clinic input carries `@critical_section` (`Modules/_io/bufferedio.c:611-614`), so the generated wrapper runs the impl under `Py_BEGIN_CRITICAL_SECTION(self)` (`Modules/_io/clinic/bufferedio.c.h:410-412`) and its field writes are protected:

```c
raw = self->raw;
self->raw = NULL;        // :626
self->detached = 1;      // :627
self->ok = 0;            // :628   <-- the write TSan flags (size 4)
```

But the `tp_iternext` slot `buffered_iternext` is **not** a clinic function. It performs its initialization check at the top of the function, outside any critical section, and only opens a critical section later, around the internal `_buffered_readline` helper:

```c
static PyObject *
buffered_iternext(PyObject *op)
{
    buffered *self = buffered_CAST(op);
    ...
    CHECK_INITIALIZED(self);                 // :1504  <-- reads self->ok (size 4), NO critical section
    ...
    if (tp == state->PyBufferedReader_Type ||
        tp == state->PyBufferedRandom_Type)
    {
        /* Skip method call overhead for speed */
        Py_BEGIN_CRITICAL_SECTION(self);     // :1512  <-- CS starts HERE (added by PR #150295)
        line = _buffered_readline(self, -1);
        Py_END_CRITICAL_SECTION();
    }
```

`CHECK_INITIALIZED` reads `self->ok` first:

```c
#define CHECK_INITIALIZED(self) \
    if (self->ok <= 0) { \                    // :341  <-- the racing read
        if (self->detached) { ... } ...
```

So one thread iterating a shared `BufferedReader` (`next()` / `for` / `list()`) reads `self->ok` at `:1504` with no lock, while another thread calling `reader.detach()` writes `self->ok = 0` at `:628` under the critical section. Reader (plain load) vs writer (CS-protected store) on the same `int` field is a TSan data race. The two struct fields are `PyObject *raw; int ok; int detached;` (`bufferedio.c:226-228`); the raced word is `self->ok`.

## Reproducer

```python
import io, sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NR = 3                         # iterator threads: for _ in obj  (buffered_iternext CHECK_INITIALIZED)
ND = 3                         # detacher threads: obj.detach()  (self->ok = 0 under CS)
ROUNDS = 4000
DATA = b"line\n" * 4000        # enough lines that iteration stays busy while detach fires

box = [None]
enter = threading.Barrier(NR + ND + 1)
leave = threading.Barrier(NR + ND + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        try:
            for _line in obj:      # -> buffered_iternext -> CHECK_INITIALIZED reads self->ok (:1504)
                pass
        except (ValueError, OSError):
            pass
        leave.wait()

def detacher():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        try:
            obj.detach()           # under CS: self->raw=NULL; self->detached=1; self->ok=0 (:628)
        except (ValueError, OSError):
            pass
        leave.wait()

threads = ([threading.Thread(target=reader) for _ in range(NR)] +
           [threading.Thread(target=detacher) for _ in range(ND)])
for t in threads:
    t.start()

for r in range(ROUNDS):
    raw = io.BytesIO(DATA)
    box[0] = io.BufferedReader(raw)   # fresh shared object each round; ok=1, detached=0
    enter.wait()
    leave.wait()
for t in threads:
    t.join()
print("done, no crash")
```

`detach()` is one-shot (it invalidates the object), so each round uses a **fresh** shared `BufferedReader`, lined up on a barrier and raced by iterator + detacher threads so the `buffered_iternext` flag-read and the `detach` flag-write overlap on the *same* object. This reaches `buffered_iternext` via the `for _line in obj` loop (`_PyForIter_VirtualIteratorNext`); the fleet vehicle reached the identical site via `list(reader)` (`list_extend_iter_lock_held`) — same two racing frames.

Run (free-threaded + TSan build):

```sh
setarch -R env -u PYTHON_GIL PYTHON_GIL=0 \
  TSAN_OPTIONS='halt_on_error=1:symbolize=1:exitcode=66:history_size=4' \
  DEBUGINFOD_URLS= \
  bash -c 'ulimit -v unlimited; exec .../debug-ft-nojit-tsan/python repro.py'
```

**Reliability: 8/8 runs exit 66**, deterministically, in ~0.6 s each. The `SUMMARY` names either side of the same read/write pair across runs (`_io__Buffered_detach_impl:628` or `buffered_iternext:1504`).

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race
  Write of size 4 at 0x7fffb68547f8 by thread T5:
    #0 _io__Buffered_detach_impl  Modules/_io/bufferedio.c:628:14
    #1 _io__Buffered_detach       Modules/_io/clinic/bufferedio.c.h:411:20   (Py_BEGIN_CRITICAL_SECTION wrapper)
    ... method_vectorcall ... thread_run

  Previous read of size 4 at 0x7fffb68547f8 by thread T2:
    #0 buffered_iternext          Modules/_io/bufferedio.c:1504:5            (CHECK_INITIALIZED, no CS)
    #1 _PyForIter_VirtualIteratorNext Python/ceval.c:3774:22
    ... _PyEval_EvalFrameDefault ... thread_run

SUMMARY: ThreadSanitizer: data race Modules/_io/bufferedio.c:628:14 in _io__Buffered_detach_impl
```

The fleet vehicle's stanza is equivalent — same `Write _io__Buffered_detach_impl:628` vs `Read buffered_iternext:1504` pair — but reached the read through `list(reader)`:

```
  Previous read of size 4 ... by thread T5:
    #0 buffered_iternext          Modules/_io/bufferedio.c:1504:5
    #1 list_extend_iter_lock_held Objects/listobject.c:1318:26
    #2 _list_extend               Objects/listobject.c:1507:15
    #3 list___init___impl         Objects/listobject.c:3539:13   (list(reader))
```

## Root cause

This is a *residual* of an incompletely-fixed known race. gh-149816 ("22 free-threading race conditions") item #84 — "Iterator path bypasses buffered object lock in `Modules/_io/bufferedio.c`" — was addressed by **PR #150295** (merged commit `e8545ed3`, 2026-05-23), whose entire `bufferedio.c` change was:

```diff
@@ buffered_iternext @@
         /* Skip method call overhead for speed */
+        Py_BEGIN_CRITICAL_SECTION(self);
         line = _buffered_readline(self, -1);
+        Py_END_CRITICAL_SECTION();
```

That closed the race on the readline *body* (the buffer/`self->raw` accesses inside `_buffered_readline`, which now runs mutually-excluded with `detach`/`close`/`read`). But the `CHECK_INITIALIZED(self)` at the **top** of `buffered_iternext` (`:1504`) was left *outside* the new critical section, so its `self->ok` read still races `detach()`'s `self->ok = 0`. The fix commit is present in the build tree (`git merge-base --is-ancestor e8545ed3 HEAD` = yes) yet the repro still fires — because the leading flag read was never brought under the lock.

The sibling clinic methods do not have this problem: `seekable`, `readable`, `writable`, `flush`, `closed`, `detach`, etc. are all `@critical_section` clinic functions, so *their* `CHECK_INITIALIZED(self)` runs inside the wrapper's critical section. Only the two non-clinic `tp_iternext` slots (`buffered_iternext` here; `stringio_iternext` in the sibling #153296) check-then-act outside the lock.

## Impact / severity

**Low as a pure data race, with latent NULL-deref potential.**

- The *reported* race is on `self->ok`, a single aligned `int` that is only ever `0` or `1`; the read cannot tear and the value is benign, so the immediate TSan finding is value-benign like most flag races.
- However, the missing-CS window is a genuine **TOCTOU**: a reader can read `self->ok == 1` (stale) at `:1504`, pass `CHECK_INITIALIZED`, then `detach()` completes (`self->raw = NULL`), then the reader enters its critical section at `:1512` and runs `_buffered_readline` against the just-detached object. `_buffered_readline`'s first act is `CHECK_CLOSED(self, ...)` -> `IS_CLOSED(self)` (`bufferedio.c:364-368`), which — because `self->buffer` is *not* freed by `detach` — evaluates `self->fast_closed_checks ? _PyFileIO_closed(self->raw) : buffered_closed(self)`. **Both branches dereference `self->raw`, now NULL.** So the same gap can escalate to a NULL-deref crash. This is the exact shape of the sibling **#153296** (`stringio_iternext` racing `write`/`seek`), which is filed as `type-crash` (a confirmed UAF/segfault). Here the readline body is now CS-protected, so the escalation is narrower (the check-then-act on `self->ok`/`self->raw`), and this repro halts on the benign flag race first (`halt_on_error=1`) rather than observing the crash.

Net: a real, easily-triggered TSan data race on `next(reader)` / `for line in reader` — operations user code treats as thread-safe reads on a shared object — and a residual of a fix the tracker already considers done.

## Real bug vs. expected

**Real CPython free-threading bug, in scope, and a residual of an incomplete fix.** Not "don't share the object": `detach()` was deliberately made `@critical_section`, and the iterator path was explicitly hardened by PR #150295 — the maintainers already decided this pair must be lock-safe. The fix simply didn't extend the critical section over the leading `CHECK_INITIALIZED`. This is a normal method-vs-method race (iterate vs detach), distinct from concurrent `__init__`/construction (cf. cpython#127192, out of scope). Falls squarely in the gh-116738 "audit built-in modules for thread safety" / gh-149816 remit.

## Suggested fix

Bring `CHECK_INITIALIZED` (and the `state`/`tp` reads it precedes) under the critical section, or, minimally, widen the existing critical section so the whole fast-path check-and-read is atomic with respect to `detach`/`close`. For example:

```c
    _PyIO_State *state = find_io_state_by_def(Py_TYPE(self));
    tp = Py_TYPE(self);
    if (tp == state->PyBufferedReader_Type ||
        tp == state->PyBufferedRandom_Type)
    {
        Py_BEGIN_CRITICAL_SECTION(self);
        CHECK_INITIALIZED(self);          /* now inside the CS; see note re: return-in-macro */
        line = _buffered_readline(self, -1);
        Py_END_CRITICAL_SECTION();
    }
```

(`CHECK_INITIALIZED` does `return NULL` on failure, which cannot appear inside `Py_BEGIN/END_CRITICAL_SECTION`; the check would need to set `line = NULL` / `goto` out of the section, or the whole function be re-expressed. The simplest robust option is to hold the critical section across the check for both the fast and slow branches.) The non-free-threaded build is unaffected (critical sections compile to no-ops there).

## Notes

- Found by ThreadSanitizer fuzzing (`fusil --tsan`); vehicle module `tempfile` (which builds `BufferedReader`s over temp files and iterates/`list()`s them). Reproduced synthetically over `io.BufferedReader(io.BytesIO(...))`.
- The raced field is `self->ok` (`bufferedio.c:227`), read by `CHECK_INITIALIZED` at `:341`; the write is `self->ok = 0` at `:628`. Size 4 confirms `int ok`, not the size-8 `PyObject *raw`.
- Same *class* as #153296 (`stringio_iternext` — a non-clinic `tp_iternext` slot doing check/read outside the per-object CS that a mutating method holds) but a different type/file (`io.StringIO` / `stringio.c`), so a distinct finding.
- Distinct from #151707 (`FileIO` fd races) — that is `Modules/_io/fileio.c` on the *raw* stream's file descriptor; this is `bufferedio.c` on the *buffered* wrapper's `self->ok` flag.
- #144380/#144381 fixed an unrelated correctness bug (the fast-path type check in `buffered_iternext`); it is not this race, but note that the race lives on that same fast path — and the `CHECK_INITIALIZED` at `:1504` executes on *both* the fast and slow paths, so the race is independent of which branch is taken.

---

*Status: reported (covered by gh-149816 item #84 / PR #150295) but the merged fix is incomplete — this residual is still reproducible on `main` (`bcf98ddbc40`). Part of the `fusil --tsan` umbrella tracking; flag as an incomplete-fix follow-up to PR #150295.*
