# Data race: a shared `pyexpat`/`_elementtree` parser's reparse-deferral flag is written and read without synchronization (`expat/xmlparse.c:3035` vs `:1136`)

*`XML_SetReparseDeferralEnabled()` stores the bundled-libexpat parser's `m_reparseDeferralEnabled` byte with a plain write (`xmlparse.c:3035`), while `callProcessor()` — reached on every `Parse()` — reads that same byte with a plain load (`xmlparse.c:1136`). Neither the CPython `pyexpat` wrapper nor `_elementtree` takes a per-parser lock, so a single parser object shared across threads that mixes `SetReparseDeferralEnabled()`/`flush()` with `Parse()`/`close()` is a TSan data race on that flag. The race is value-benign (a 1-byte bool, both values valid) — but an expat parser is a single-threaded object by design, so this is really one symptom of "don't share a parser across threads", not an internal cache that looks read-only.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

The bundled libexpat parser struct caches whether reparse deferral is enabled in a plain 1-byte field:

```c
/* Modules/expat/xmlparse.c:689 */
XML_Bool m_reparseDeferralEnabled;
```

`XML_SetReparseDeferralEnabled()` writes it unconditionally, with no memory ordering:

```c
/* Modules/expat/xmlparse.c:3032 */
XML_Bool XMLCALL
XML_SetReparseDeferralEnabled(XML_Parser parser, XML_Bool enabled) {
  if (parser != NULL && (enabled == XML_TRUE || enabled == XML_FALSE)) {
    parser->m_reparseDeferralEnabled = enabled;   /* :3035  write (size 1) */
    return XML_TRUE;
  }
  return XML_FALSE;
}
```

`callProcessor()` — invoked on every `XML_Parse()`/`XML_ParseBuffer()` — reads it, also plain:

```c
/* Modules/expat/xmlparse.c:1131 */
static enum XML_Error
callProcessor(XML_Parser parser, const char *start, const char *end,
              const char **endPtr) {
  const size_t have_now = EXPAT_SAFE_PTR_DIFF(end, start);
  if (parser->m_reparseDeferralEnabled          /* :1136  read (size 1) */
      && ! parser->m_parsingStatus.finalBuffer) {
    ...
```

Two threads using the *same* parser object — one calling `SetReparseDeferralEnabled(...)`, the other calling `Parse(...)` — race on `m_reparseDeferralEnabled`. The CPython glue does not serialize the two operations:

- `pyexpat`: `pyexpat_xmlparser_SetReparseDeferralEnabled_impl` (`Modules/pyexpat.c:849`) → `XML_SetReparseDeferralEnabled`; `pyexpat_xmlparser_Parse_impl` (`Modules/pyexpat.c:919`) → `XML_Parse` → `callProcessor`. Neither uses `Py_BEGIN_CRITICAL_SECTION` / `PyMutex`.
- `_elementtree` (the seeding vehicle): `XMLParser.flush()` (`Modules/_elementtree.c:4002/4006`) deliberately toggles the flag `SetReparseDeferralEnabled(FALSE) → parse → SetReparseDeferralEnabled(TRUE)` on the internal parser, while `XMLParser.close()` → `expat_parse` (`:3924/:3960`) → `XML_Parse` → `callProcessor` reads it. Again unsynchronized.

## Reproducer

```python
import sys, threading
import pyexpat
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# A pyexpat parser caches "reparse deferral" in the plain 1-byte XML_Bool field
# m_reparseDeferralEnabled (Modules/expat/xmlparse.c:689). SetReparseDeferralEnabled()
# writes it (xmlparse.c:3035) with no lock, while Parse() -> XML_ParseBuffer ->
# callProcessor reads it (xmlparse.c:1136). A parser shared across threads that mixes
# SetReparseDeferralEnabled() with Parse() races on that byte.
NT = 6                     # 1 parser-driver + (NT-1) flag-writers
ROUNDS = 4000
pool = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

# Well-formed-so-far prefix (root never closed, isfinal=0) so repeated Parse() calls on
# one parser never error -- callProcessor still reads the flag on every call.
CHUNK = b"<r>" + b"<e a='1'/>" * 8

def worker(wid):
    for _ in range(ROUNDS):
        enter.wait()
        p = pool[0]
        if wid == 0:
            for _ in range(6):
                try:
                    p.Parse(CHUNK, 0)        # -> callProcessor: read m_reparseDeferralEnabled
                except Exception:
                    pass
        else:
            for k in range(6):
                try:
                    p.SetReparseDeferralEnabled(bool((wid + k) & 1))  # write m_reparseDeferralEnabled
                except Exception:
                    pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts: t.start()
for r in range(ROUNDS):
    pool[0] = pyexpat.ParserCreate()          # fresh parser each round (first-touch)
    enter.wait(); leave.wait()
for t in ts: t.join()
print("done, no crash")
```

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, libexpat 2.8.1, Clang 21)

```
WARNING: ThreadSanitizer: data race (pid=1964184)
  Write of size 1 at 0x7fffb61c0078 by thread T2:
    #0 PyExpat_XML_SetReparseDeferralEnabled Modules/expat/xmlparse.c:3035:38   (parser->m_reparseDeferralEnabled = enabled)
    #1 pyexpat_xmlparser_SetReparseDeferralEnabled_impl Modules/pyexpat.c:849:5
    #2 pyexpat_xmlparser_SetReparseDeferralEnabled Modules/clinic/pyexpat.c.h:35:20
    ...
    #30 thread_run Modules/_threadmodule.c:388:21

  Previous read of size 1 at 0x7fffb61c0078 by thread T1:
    #0 callProcessor Modules/expat/xmlparse.c:1136:15   (if (parser->m_reparseDeferralEnabled && ...))
    #1 PyExpat_XML_ParseBuffer Modules/expat/xmlparse.c:2413:25
    #2 PyExpat_XML_Parse Modules/expat/xmlparse.c:2367:10
    #3 pyexpat_xmlparser_Parse_impl Modules/pyexpat.c:919:10
    #4 pyexpat_xmlparser_Parse Modules/clinic/pyexpat.c.h:109:20
    ...
    #30 thread_run Modules/_threadmodule.c:388:21

SUMMARY: ThreadSanitizer: data race Modules/expat/xmlparse.c:3035:38 in PyExpat_XML_SetReparseDeferralEnabled
```

Reproduces in <1 s and deterministically (exit 66) across repeated runs. It does not crash — the racing value is a single `XML_Bool` byte and both `XML_TRUE`/`XML_FALSE` are individually valid. (The seeding fleet vehicle drove the identical two-function signature through `_elementtree.XMLParser.flush()` vs `.close()`; this reproducer drives it directly through `pyexpat` for a smaller, loopable window.)

## Root cause

`m_reparseDeferralEnabled` is a plain `XML_Bool` (1 byte). Every access to it in the bundled expat is a plain read or write — the initializer (`:1453`), the external-entity save/restore (`:1708`/`:1764`), the setter (`:3035`), and the `callProcessor` reader (`:1136`). libexpat is a third-party library that is single-threaded by contract: one parser object is not meant to be touched by two threads at once, so expat itself never synchronizes any of its parser fields.

CPython exposes that parser through `pyexpat` (and, wrapped, through `_elementtree`). On a free-threaded (`--disable-gil`) build the GIL no longer serializes the two Python-level calls, so a `SetReparseDeferralEnabled()`/`flush()` on one thread and a `Parse()`/`close()` on another reach the plain write and the plain read of the same byte with no happens-before edge — a genuine TSan data race. The `PyExpat_` name prefix is CPython's `expat_config.h` symbol renaming; the code is bundled libexpat, not CPython-authored.

Note this one byte is only the *first-touched* shared field. Concurrent `Parse()` from two threads (or `flush()`/`feed()`/`close()` interleaving) mutates essentially the entire parser struct — buffers, tag stack, processor pointer, position counters — none of it synchronized. So this flag is one visible symptom of a broader "the parser object is not safe to share across threads" reality.

## Impact / severity

Low. The race is value-benign: `m_reparseDeferralEnabled` is a lone byte, aligned, and holds a valid bool either way; a torn/stale read only nudges the reparse-deferral *heuristic* (whether to retry a partial token now or wait for more data), never memory safety. There is no use-after-free or crash from this field itself. It requires a program to share one parser object across threads and call mutating methods on it concurrently — which is outside libexpat's documented single-threaded usage and is undefined for `xml.parsers.expat` / `xml.etree.ElementTree` parser objects regardless of this specific field.

## Suggested fix

There is no clean "one atomic" fix here, and applying one would be misleading: making just `m_reparseDeferralEnabled` atomic would silence *this* TSan report while leaving the rest of the parser struct equally unsynchronized. Two honest options:

1. **Document the contract (status quo).** Treat `pyexpat`/`_elementtree` parser objects as non-shareable across threads (as libexpat already specifies for `XML_Parser`). No code change; this race is then "working as intended — don't do that."
2. **If pyexpat is to be made crash/UB-safe under accidental sharing** (the free-threading "pure-Python code must not trigger C-level UB" goal), wrap the *mutating* parser methods (`Parse`, `ParseFile`, `SetReparseDeferralEnabled`, `flush`, `feed`, `close`, …) in a per-parser critical section (`Py_BEGIN_CRITICAL_SECTION(self)` / `PyMutex`) so calls on one parser object serialize. That fixes the whole class, not just this byte. It is a larger change and belongs upstream, not in the bundled libexpat source.

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`), fleet 01, vehicle `inst-04/python/xml_etree_ElementTree-warning_threadsanitizer_data_race-tsanNEW` (drove it via `_elementtree.XMLParser.flush()` vs `.close()`). Unlike TSAN-0005 (`dec_hash` on an immutable `Decimal`, where `hash()` genuinely looks read-only), a pyexpat parser is an explicitly stateful, single-threaded object, so this is closer to expected "don't share the parser" behavior than an interpreter bug. Likely **not worth an individual CPython issue** on its own merits; carried in the catalog as a low-severity, value-benign race and as a data point for any decision about whether pyexpat should adopt per-parser critical sections for free-threading.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
