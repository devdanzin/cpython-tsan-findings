# Data race: a shared iso2022 `MultibyteIncrementalDecoder`'s `reset()` races with `getstate()` on the codec state field (`_codecs_iso2022.c:314` / `longobject.c:1069`)

*An iso2022 `MultibyteIncrementalDecoder` keeps its codec designation state in a plain 8-byte field embedded in the object (`self->state.c`, `MultibyteCodec_State`). `reset()` rewrites it through the codec's `decreset` callback — `iso2022_decode_reset` does `STATE_SETG0(CHARSET_ASCII)`, a 1-byte write of `self->state.c[0]` (`_codecs_iso2022.c:314`) — while `getstate()` reads the whole field via `_PyLong_FromByteArray(self->state.c, …)` (byte read at `longobject.c:1069`). Neither method takes a lock or critical section, so a decoder shared across threads races on `self->state.c`. This is the **same unsynchronized-decoder-state bug as TSAN-0001, from the same fleet vehicle** — TSAN-0001 is the `pendingsize` face, this is the `state.c` (charset-designation) face; one fix covers both.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

`Modules/cjkcodecs/multibytecodec.c` implements the stateful incremental CJK decoder. Its state lives in three plain fields of `MultibyteIncrementalDecoderObject` (`Modules/cjkcodecs/multibytecodec.h`), none of which is lock-protected:

```c
#define _MultibyteStatefulCodec_HEAD            \
    PyObject_HEAD                               \
    const MultibyteCodec *codec;                \
    MultibyteCodec_State state;                 \   /* 8 bytes: unsigned char c[8] */
    PyObject *errors;
#define _MultibyteStatefulDecoder_HEAD          \
    _MultibyteStatefulCodec_HEAD                \
    unsigned char pending[MAXDECPENDING];       \
    Py_ssize_t pendingsize;
```

For the iso2022 codecs, `state.c` holds the ISO-2022 shift/designation state (`c[0..3]` = the G0–G3 charsets, `c[4]` = flags). `reset()` clears it, `getstate()` serialises it, `decode()`/`setstate()` mutate it — all without synchronization. The file contains **zero** `Py_BEGIN_CRITICAL_SECTION` uses and the Argument Clinic inputs are not `@critical_section`, so two threads sharing one iso2022 decoder — one calling `reset()`, another `getstate()` — race on `self->state.c[0]`.

The reported pairing (a 1-byte write of `state.c[0]` vs a 1-byte read of the same byte) is value-benign, but it is a genuine TSan-reported data race on methods callers treat as innocuous, and the broader family (`decode()`/`setstate()` mutating `state` + `pending` + `pendingsize` as a unit) is the real hazard — see TSAN-0001.

## Reproducer

```python
import sys, threading, codecs
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# One MultibyteIncrementalDecoder for an iso2022 codec keeps its 8-byte codec state
# inline in the object (self->state.c). Sharing it across threads is a data race:
#   .reset()    -> iso2022_decode_reset writes self->state.c[0] (_codecs_iso2022.c:314)
#   .getstate() -> _PyLong_FromByteArray reads self->state.c    (longobject.c:1069)
# Neither takes a lock / critical section. We use ONE writer thread (reset) so the
# only possible race partner for the state.c write is a reader's _PyLong_FromByteArray,
# and several reader threads (getstate) to widen the window.
NT_READERS = 5         # getstate readers
ROUNDS = 4000
INNER = 8
pool = [None]
enter = threading.Barrier(NT_READERS + 1 + 1)   # readers + 1 writer + main
leave = threading.Barrier(NT_READERS + 1 + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        d = pool[0]
        for _ in range(INNER):
            d.getstate()           # _PyLong_FromByteArray: read self->state.c  (:1069)
        leave.wait()

def writer():
    for _ in range(ROUNDS):
        enter.wait()
        d = pool[0]
        for _ in range(INNER):
            d.reset()              # iso2022_decode_reset: write self->state.c[0]  (:314)
        leave.wait()

ts = [threading.Thread(target=reader) for _ in range(NT_READERS)]
ts.append(threading.Thread(target=writer))
for t in ts: t.start()
for r in range(ROUNDS):
    d = codecs.getincrementaldecoder("iso2022_jp_2")()
    d.decode(b"\x1b$B")            # designate JISX0208 as G0 so reset() actually mutates c[0]
    pool[0] = d                   # fresh, shared decoder each round
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, Clang 21)

```
WARNING: ThreadSanitizer: data race (pid=1966658)
  Write of size 1 at 0x7fffb6555448 by thread T6:
    #0 iso2022_decode_reset Modules/cjkcodecs/_codecs_iso2022.c:314:5
    #1 _multibytecodec_MultibyteIncrementalDecoder_reset_impl Modules/cjkcodecs/multibytecodec.c:1344:9
    #2 _multibytecodec_MultibyteIncrementalDecoder_reset Modules/cjkcodecs/clinic/multibytecodec.c.h:466:12
    #3 method_vectorcall_NOARGS Objects/descrobject.c:448:24
    ...
    #29 thread_run Modules/_threadmodule.c:388:21

  Previous read of size 1 at 0x7fffb6555448 by thread T1:
    #0 _PyLong_FromByteArray Objects/longobject.c:1069:34
    #1 _multibytecodec_MultibyteIncrementalDecoder_getstate_impl Modules/cjkcodecs/multibytecodec.c:1266:29
    #2 _multibytecodec_MultibyteIncrementalDecoder_getstate Modules/cjkcodecs/clinic/multibytecodec.c.h:420:12
    #3 method_vectorcall_NOARGS Objects/descrobject.c:448:24
    ...
    #29 thread_run Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/cjkcodecs/_codecs_iso2022.c:314:5 in iso2022_decode_reset
```

Reproduces within a second or two (exit 66). The confirmed signature matches the fleet-seeded one exactly (write `iso2022_decode_reset:314` vs read `_PyLong_FromByteArray:1069`, same address). Across 15 runs, ~12 caught this exact pairing; the remainder caught two adjacent faces of the **same** object-state race: a `reset` write vs `reset` write on `state.c[0]` (both `iso2022_decode_reset:314`), and the `pendingsize` face (`getstate_impl:1261` read vs `reset_impl:1346` write) that TSAN-0001 documents. It does not crash — the racing byte is being set to a fixed value (`'B'` = ASCII).

## Root cause

`reset()` clears the decoder state through the codec's `decreset` callback (`multibytecodec.c:1339-1349`):

```c
static PyObject *
_multibytecodec_MultibyteIncrementalDecoder_reset_impl(MultibyteIncrementalDecoderObject *self)
{
    if (self->codec->decreset != NULL &&
        self->codec->decreset(&self->state, self->codec) != 0)   /* :1344 -> iso2022_decode_reset */
        return NULL;
    self->pendingsize = 0;                                       /* :1346  write pendingsize */
    Py_RETURN_NONE;
}
```

For iso2022, `decreset` is `iso2022_decode_reset` (`_codecs_iso2022.c:312-317`), which writes `self->state.c[0]` and `self->state.c[4]`:

```c
DECODER_RESET(iso2022)
{
    STATE_SETG0(CHARSET_ASCII);   /* :314  ((state)->c[0]) = 'B'  -- 1-byte write */
    STATE_CLEARFLAG(F_SHIFTED);   /* :315  ((state)->c[4]) &= ~0x01 */
    return 0;
}
```

`getstate()` reads the same `state.c` as an integer (`multibytecodec.c:1253-1276`):

```c
static PyObject *
_multibytecodec_MultibyteIncrementalDecoder_getstate_impl(MultibyteIncrementalDecoderObject *self)
{
    ...
    statelong = (PyObject *)_PyLong_FromByteArray(self->state.c,   /* :1266  read self->state.c */
                                                  sizeof(self->state.c), 1, 0);
    ...
}
```

`_PyLong_FromByteArray` walks the byte array (`longobject.c:1068-1069`): `for (...) { twodigits thisbyte = *p; ... }`, so the `*p` at `:1069` is the 1-byte read of `self->state.c[i]` that races the reset write of `self->state.c[0]`.

Under the GIL these are mutually atomic; under free-threading they run concurrently on the same `self` with no synchronization. `self->state` (like `self->pending`/`self->pendingsize`) is a plain member accessed with no atomics and no critical section — `multibytecodec.c` has **zero** `Py_BEGIN_CRITICAL_SECTION` and no `@critical_section` clinic annotations. `decode()` also mutates `state.c` byte-by-byte while processing escape sequences, and `setstate()` does `memcpy(self->state.c, statebytes, 8)` (`:1330`), so the race is a whole-object property of the decoder's mutable state, not specific to the reset/getstate pair the fuzzer hit.

**Relationship to TSAN-0001.** This is the same bug as TSAN-0001, found from the *same fleet vehicle* (`inst-03/python/encodings_iso2022_jp_2-…-tsanNEW`). TSAN-0001 is the `pendingsize` field face (`getstate_impl:1261` read vs `reset_impl:1346` write); TSAN-0004 is the `state.c` field face (`iso2022_decode_reset:314` write vs `_PyLong_FromByteArray:1069` read). `fusil`'s tsan_dedup keyed them as two signatures because the racing *function* pairs differ, but they are two adjacent fields of one unguarded `MultibyteIncrementalDecoderObject`, fixed by the same per-object critical section.

## Impact / severity

- **Severity: low–medium.** The reported `reset` write vs `getstate` read of `state.c[0]` is value-benign and crash-free (a 1-byte store of a constant). But it is a real, reportable data race on ostensibly innocuous methods.
- The wider concern in the same family is `decode()`/`setstate()`, which mutate `state` + `pending` + `pendingsize` (the latter via `memcpy` into a fixed 8-byte `pending[]` buffer keyed on `pendingsize`). Concurrent decode/setstate/reset vs getstate on a shared decoder can tear those fields against each other — a torn/inconsistent state readout, and for the `pending`/`pendingsize` pair a memory-safety hazard (length read against a buffer/length write mid-flight). Worth auditing as one fix (see TSAN-0001).
- Trigger requires *sharing one decoder object across threads*, arguably user misuse — but codec objects look immutable/stateless to callers (a decoder is commonly cached and reused), so this is exactly the "looks safe to share, secretly isn't" class the free-threading effort targets.

## Suggested fix

Same fix as TSAN-0001: give the incremental encoder/decoder (and stream reader/writer) methods per-object critical sections so their access to `state`/`pending`/`pendingsize` is serialized. Cleanest via Argument Clinic annotation:

```
/*[clinic input]
@critical_section
_multibytecodec.MultibyteIncrementalDecoder.reset
[clinic start generated code]*/
```

applied to `decode`, `getstate`, `setstate`, and `reset` (and the encoder/stream analogues). Clinic then wraps each generated call in `Py_BEGIN_CRITICAL_SECTION(self)` / `Py_END_CRITICAL_SECTION()`. A lone atomic on any single field is *not* sufficient, because `state`, `pending`, and `pendingsize` form one logical state that must be read/written as a unit (getstate serialises the codec state + the pending buffer + its length together); only a critical section / lock covers the whole tuple.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, vehicle `inst-03/python/encodings_iso2022_jp_2-…-tsanNEW` — the same vehicle that seeded TSAN-0001 (the fuzzer shared one `IncrementalDecoder()` across worker threads calling `dir()`-discovered methods, hitting `reset`/`getstate` concurrently; both the `state.c` and `pendingsize` faces fired). **This is not an independent bug from TSAN-0001** — it is a second racing field of the same unsynchronized `MultibyteIncrementalDecoder`, and the same whole-family critical-section fix resolves both. Resembles the broad "stateful stdlib C object never made FT-safe" class rather than a specific already-filed bug. If filed, TSAN-0001 and TSAN-0004 should be a single report (or two data points in one) covering `multibytecodec.c`'s incremental/stream CJK codec family.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
