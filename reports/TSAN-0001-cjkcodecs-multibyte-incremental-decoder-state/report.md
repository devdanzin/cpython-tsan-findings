# Data race: a shared `MultibyteIncrementalDecoder`'s `getstate()` races with `reset()` on the decoder's `pendingsize` state (`multibytecodec.c:1261` / `:1346`)

*A `MultibyteIncrementalDecoder` (any CJK incremental codec, e.g. `euc_jp`, `iso2022_jp`) keeps its incomplete-input state in plain, unsynchronized C fields — `unsigned char pending[8]`, `Py_ssize_t pendingsize`, and `MultibyteCodec_State state`. `getstate()` reads `self->pending`/`self->pendingsize`; `reset()` writes `self->pendingsize = 0`. Neither method takes a lock or a critical section, so a decoder shared between threads that call `getstate()` and `reset()` concurrently is a data race on `self->pendingsize`. These methods look independent and read-only-ish to callers, but they mutate hidden per-object state.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

> **Tracked in the umbrella issue [python/cpython#153852](https://github.com/python/cpython/issues/153852)** — one of a batch of free-threading data races found with `fusil --tsan`.

## Summary

`Modules/cjkcodecs/multibytecodec.c` implements the stateful incremental CJK decoder. Its incomplete multibyte sequence is buffered in three plain fields of `MultibyteIncrementalDecoderObject` (`Modules/cjkcodecs/multibytecodec.h`):

```c
#define MAXDECPENDING   8
#define _MultibyteStatefulDecoder_HEAD          \
    _MultibyteStatefulCodec_HEAD                \
    unsigned char pending[MAXDECPENDING];       \
    Py_ssize_t pendingsize;
```

`getstate()` reads `pending`/`pendingsize`; `reset()` and `setstate()`/`decode()` write them. None of these methods is guarded by the GIL (free-threaded build) or by a per-object critical section — the file contains **zero** `Py_BEGIN_CRITICAL_SECTION` uses and the Argument Clinic inputs are not annotated `@critical_section`. Two threads sharing one decoder — one calling `getstate()`, another `reset()` — race on `self->pendingsize` (an aligned 8-byte `Py_ssize_t`).

It is (in this getstate-vs-reset pairing) value-benign — the store is a single aligned word — but it is a genuine TSan-reported data race, and the broader family is not benign: `decode()`/`setstate()` `memcpy` into the fixed 8-byte `pending[]` buffer using an attacker-influenced `pendingsize`, so unsynchronized concurrent decode/setstate on a shared decoder can tear the buffer and length against each other.

## Reproducer

```python
import sys, threading, codecs
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NT = 8
ROUNDS = 4000
box = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        dec = box[0]
        for _ in range(6):
            try:
                dec.getstate()      # reads self->pendingsize (multibytecodec.c:1261)
            except Exception:
                pass
            try:
                dec.reset()         # writes self->pendingsize = 0 (multibytecodec.c:1346)
            except Exception:
                pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts:
    t.start()

factory = codecs.getincrementaldecoder("euc_jp")
for r in range(ROUNDS):
    dec = factory()
    dec.decode(b"\xa4")             # partial euc_jp lead byte -> pendingsize=1, pending non-empty
    box[0] = dec                    # publish the shared decoder for this round
    enter.wait()
    leave.wait()
for t in ts:
    t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, Clang 21)

```
WARNING: ThreadSanitizer: data race (pid=1962131)
  Read of size 8 at 0x7fffb6c40220 by thread T1:
    #0 _multibytecodec_MultibyteIncrementalDecoder_getstate_impl Modules/cjkcodecs/multibytecodec.c:1261:46
    #1 _multibytecodec_MultibyteIncrementalDecoder_getstate     Modules/cjkcodecs/clinic/multibytecodec.c.h:420:12
    #2 method_vectorcall_NOARGS Objects/descrobject.c:448:24
    ...
    #29 thread_run Modules/_threadmodule.c:388:21

  Previous write of size 8 at 0x7fffb6c40220 by thread T5:
    #0 _multibytecodec_MultibyteIncrementalDecoder_reset_impl   Modules/cjkcodecs/multibytecodec.c:1346:23
    #1 _multibytecodec_MultibyteIncrementalDecoder_reset        Modules/cjkcodecs/clinic/multibytecodec.c.h:466:12
    #2 method_vectorcall_NOARGS Objects/descrobject.c:448:24
    ...
    #29 thread_run Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/cjkcodecs/multibytecodec.c:1261:46 in _multibytecodec_MultibyteIncrementalDecoder_getstate_impl
```

Reproduces deterministically within a second or two (exit 66). The confirmed signature matches the fleet-seeded one exactly (same two functions; the read is `getstate_impl:1261`, the write is `reset_impl:1346`). It does not crash in this getstate/reset pairing — the racing value is a single aligned word.

## Root cause

`getstate()` reads the pending state (`Modules/cjkcodecs/multibytecodec.c`):

```c
static PyObject *
_multibytecodec_MultibyteIncrementalDecoder_getstate_impl(MultibyteIncrementalDecoderObject *self)
{
    ...
    buffer = PyBytes_FromStringAndSize((const char *)self->pending,
                                       self->pendingsize);   /* :1261  read pending/pendingsize */
    ...
    statelong = _PyLong_FromByteArray(self->state.c, ...);   /* also reads self->state */
    return Py_BuildValue("NN", buffer, statelong);
}
```

`reset()` writes it:

```c
static PyObject *
_multibytecodec_MultibyteIncrementalDecoder_reset_impl(MultibyteIncrementalDecoderObject *self)
{
    if (self->codec->decreset != NULL &&
        self->codec->decreset(&self->state, self->codec) != 0)   /* writes self->state */
        return NULL;
    self->pendingsize = 0;                                       /* :1346  write pendingsize */
    Py_RETURN_NONE;
}
```

Under the GIL these are atomic with respect to each other; under free-threading they run on different threads with no synchronization on `self`. TSan flags the concurrent read (`:1261`) and write (`:1346`) of the 8-byte `self->pendingsize`. The same unprotected fields are also written by `decode()` and `setstate()` (`multibytecodec.c:1328-1330`: `self->pendingsize = buffersize; memcpy(self->pending, ...); memcpy(self->state.c, ...);`), so the race is a whole-object property of the decoder's mutable state, not specific to the getstate/reset pair the fuzzer happened to hit.

This is the standard free-threading gap for a stateful C object: the object carries mutable internal state but its methods predate PEP 703 and were never given per-object locking. `getstate`/`setstate`/`reset`/`decode` on `MultibyteIncrementalEncoderObject` and `MultibyteStreamReader`/`Writer` share the same `_MultibyteStatefulDecoder_HEAD`/`..Encoder_HEAD` layout and the same unguarded access pattern.

## Impact / severity

- **Severity: low–medium.** The specific reported race (`getstate` read vs `reset` write of `pendingsize`) is value-benign and crash-free — an aligned word store. But it is a real, reportable data race on ostensibly innocuous methods.
- The wider concern in the same family is `decode()`/`setstate()`, which `memcpy` into the fixed 8-byte `pending[]` using `pendingsize`. Concurrent decode/setstate/reset on a shared decoder can tear `pending` against `pendingsize` (a length read on one thread with a buffer/length write mid-flight), which is a memory-safety hazard, not merely value-benign — worth auditing as part of the same fix.
- Trigger requires *sharing one decoder object across threads*, which is arguably user misuse — but codec objects look immutable/stateless to callers (a decoder is commonly cached and reused), so this is exactly the "looks safe to share, secretly isn't" class the free-threading effort is meant to close.

## Suggested fix

Give the incremental encoder/decoder (and the stream reader/writer) methods per-object critical sections so their access to `pending`/`pendingsize`/`state` is serialized. The cleanest path is Argument Clinic annotation:

```
/*[clinic input]
@critical_section
_multibytecodec.MultibyteIncrementalDecoder.getstate
[clinic start generated code]*/
```

applied to `decode`, `getstate`, `setstate`, and `reset` (and the encoder/stream analogues). Clinic then wraps each generated call in `Py_BEGIN_CRITICAL_SECTION(self)` / `Py_END_CRITICAL_SECTION()`. Equivalently, wrap the bodies by hand:

```c
Py_BEGIN_CRITICAL_SECTION(self);
buffer = PyBytes_FromStringAndSize((const char *)self->pending, self->pendingsize);
...
Py_END_CRITICAL_SECTION();
```

A relaxed atomic on `pendingsize` alone is *not* sufficient here, because `pending`, `pendingsize`, and `state` form one logical state that must be read/written as a unit (getstate serializes the buffer + its length + the codec state); only a critical section / lock covers the whole tuple.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, vehicle `inst-03/python/encodings_iso2022_jp_2-...-tsanNEW` (the fuzzer shared one `IncrementalDecoder()` across worker threads calling `dir()`-discovered methods, hitting `getstate`/`reset` concurrently). Resembles the broad class of "stateful stdlib C object with no critical sections yet" free-threading races rather than a specific already-filed bug. The fix should cover the whole `multibytecodec.c` incremental/stream family, since they share the state layout and the unguarded-access pattern.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
