# Data race: a shared t-string `TemplateIter` advances `from_strings` + sub-iterators non-atomically (`templateiter_next`, `Objects/templateobject.c`) — value-benign, trust-boundary preserved

*The PEP 750 t-string template iterator (`templateiterobject`, from `iter(t"…")`) alternates between the template's static strings and its interpolations, tracking which via a plain `self->from_strings` int and advancing `self->stringsiter` / `self->interpolationsiter`. `templateiter_next` writes `from_strings` and `PyIter_Next`s the shared sub-iterators with no synchronization, so a shared `TemplateIter` driven by multiple threads races. Because t-strings are positioned for SQL/HTML sanitization, we checked the security angle explicitly: the race **reorders / loses / duplicates** items, but it does **not** turn an untrusted interpolation into trusted static — no injection.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer and the security analysis; the maintainer reviewed and edited it._

## Summary

`templateiterobject` holds `from_strings` (a plain `int`) and two sub-iterators. `templateiter_next` (`Objects/templateobject.c`) reads/writes `from_strings` (`:25`/`:31`/`:36`) and pulls from the shared sub-iterators, all unsynchronized. A single `TemplateIter` shared across threads races the flag and the cursors → the consumed sequence corrupts.

## Security analysis (`security_analysis.py`)

t-strings exist so a renderer can trust the **static** parts of a template literal and escape the **interpolations** (the untrusted `{expr}` values) — the intended defense for SQL/HTML. The concern: could a concurrency race make an untrusted value be treated as trusted static (an injection)?

The analysis drives a renderer that trusts strings and escapes interpolations, iterating one **shared** `TemplateIter` from 12 threads, and compares against the single-threaded reference. Result:

```
corrupted (reordered) iterations: 245
iterations with items LOST/DUPLICATED (multiset changed): 23
TRUST CONFUSION (interpolation value seen as static str): 0  <-- 0 = no injection
  rendered: S0[esc:U0]S1[esc:U1]S2[esc:U2][esc:U3]S4[esc:U4]S5[esc:U5]S6S3
```

So the race **does** corrupt order and completeness (items reordered, and sometimes dropped or duplicated → a mangled render), **but** every item keeps its Python type: an `Interpolation` is always an `Interpolation` and a static is always a `str`. A renderer's `isinstance(item, str)` classification is therefore robust to the race — **0 trust confusions** across 245 corrupted iterations. The untrusted values are still escaped; there is no injection / trust-boundary bypass.

## Impact / severity

**Low — value-benign for the trust model.** No memory unsafety (the `Py_SETREF` is on a local `item`, so no double-free like the GenericAlias iterator TSAN-0045). No injection (trust boundary preserved). The only effect is a mangled render, and only under an unusual pattern: sharing one template **iterator** across threads. A template is iterated once, in one thread; and position-based access (`template.strings` / `template.interpolations`) doesn't use the iterator at all. Free-threaded build only.

## Reproducer / demonstration

- `repro.py` — shares one `iter(t"…")` across threads (the TSan data-race driver).
- `security_analysis.py` — the trust-boundary check above (reorder/loss/dup vs trust confusion).

## Suggested fix

Per CPython's iterator free-threading strategy ([gh-124397](https://github.com/python/cpython/issues/124397)) this value-benign class is deemed acceptable — a shared iterator should be locked by the user. If ever hardened: atomic `from_strings` + a per-iterator critical section over the sub-iterator advance.

## Notes

- **Not fileable.** Value-benign shared-iterator class (gh-120496 closed as acceptable per gh-124397); the t-string iterator is a new member. The security check was the reason to dig in, and it came back clean (no injection). Cataloged for dedup.
- Found in a downloaded remote fleet (`magalu`, `3.16_ft_debug_tsan`).

---

*New iterator type, value-benign; trust boundary verified intact. Recorded for the catalog; not fileable.*
