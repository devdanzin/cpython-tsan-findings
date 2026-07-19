# Data race: concurrent `XMLParser.feed()/flush()/close()` on a shared parser races the single-threaded expat parser (`Modules/expat/xmlparse.c`)

*`xml.etree.ElementTree.XMLParser` (and `pyexpat.xmlparser`) wrap one libexpat parser, which is single-threaded by design. On a free-threaded build, driving **one shared** parser from multiple threads races expat's internal state with no CPython-side lock — e.g. `XMLParser.flush()` → `PyExpat_XML_SetReparseDeferralEnabled` writes parser state while `XMLParser.close()`/`feed()` → `PyExpat_XML_Parse`/`XML_ParseBuffer` → `callProcessor` advances it.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

The XMLParser/pyexpat methods call into libexpat (`XML_Parse`, `XML_ParseBuffer`, `XML_SetReparseDeferralEnabled`, …) without taking a per-parser critical section. libexpat keeps all parse state (position, buffer, processor function pointers, the reparse-deferral flag) in one `XML_Parser` struct and is not designed for concurrent calls. A shared parser driven concurrently races that struct across many faces of the parse path.

## Reproducer

```python
import threading
import xml.etree.ElementTree as ET

NT = 8
barrier = threading.Barrier(NT)


def worker(parser, role):
    barrier.wait()
    for _ in range(500):
        try:
            if role == 0:
                parser.feed(b"<a>x</a>")
            elif role == 1:
                parser.flush()
            else:
                parser.close()
        except Exception:
            pass


for _round in range(400):
    parser = ET.XMLParser()
    ts = [threading.Thread(target=worker, args=(parser, i % 3)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
```

Under a free-threaded TSan build: exit **66**, deterministically (`SUMMARY: … in PyExpat_XML_Parse`). Reproduced on our `debug-ft-nojit-tsan`; the magalu fleet drove the `flush`-vs-`close` faces (`SetReparseDeferralEnabled | callProcessor`, `XML_ParseBuffer | doProlog/errorProcessor`, …). (Full report in `tsan_report.txt`.)

## Impact / severity

**Low–moderate.** expat's internal state is corrupted under concurrent use (mis-parse, or reads of half-updated processor pointers). Sharing one parser across threads and driving it concurrently is misuse — a parser is inherently sequential — which caps priority. Free-threaded build only.

## Suggested fix

Take the parser's per-object critical section (`Py_BEGIN_CRITICAL_SECTION(self)`) around the expat calls in the XMLParser/pyexpat methods (`feed`/`flush`/`close`/`Parse`), so one shared parser is driven serially — or document that a parser must not be shared across threads. Same class of decision as the thread-unsafe-libc wrappers (cf. `localeconv`, TSAN-0047 / cpython#127081).

## Notes

- **Appears unfiled** (a `gh api` search found no XMLParser/expat FT issue). New but **low priority** — concurrent use of one sequential parser is misuse; fileable only if CPython wants XMLParser/pyexpat to lock. Not proposing a filing without maintainer interest.
- Distinct from the `_elementtree` `Element.extra` race (TSAN-0041) and TreeBuilder (TSAN-0031/0022). Found in a downloaded remote fleet (`magalu`, a `3.16_ft_debug_tsan` build), reproduced independently here.

---

*New but low-priority shared-parser race; recorded for the catalog. Not proposing a filing.*
