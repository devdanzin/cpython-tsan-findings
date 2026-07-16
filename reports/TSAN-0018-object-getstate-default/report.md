# Data race: `object.__getstate__` reads a type's shared-keys `dk_nentries` non-atomically while `setattr` atomically bumps it (`dictobject.c:7666` vs `:248`)

*`_PyObject_IsInstanceDictEmpty` (reached from `object.__getstate__` / pickle / copy) iterates a type's shared-keys with the plain loop bound `i < keys->dk_nentries`. Another thread adding a **new** attribute name via `setattr` inserts a key into those same shared keys and bumps `dk_nentries` with an **atomic** store (`split_keys_entry_added`). The writer was deliberately made atomic "when we're racing with reads", but this reader forgot the matching atomic load — so pickling/copying an object while another thread sets an attribute is a TSan data race on the type's `dk_nentries`.*

_AI Disclaimer: this report was drafted by Claude Code, which also created and ran the reproducer; the maintainer reviewed and edited it._

## Summary

On the free-threaded build, instances of an ordinary class store their attributes in *inline values* backed by the type's **shared keys** object (`CACHED_KEYS(tp)`). Two unsynchronized accesses to that shared keys object's `dk_nentries` field race:

- **Write (atomic):** `setattr(obj, new_name, v)` where `new_name` is seen for the first time by the type inserts a key into the shared keys and calls `split_keys_entry_added()`, which does `_Py_atomic_store_ssize_relaxed(&keys->dk_nentries, keys->dk_nentries + 1)` (`Objects/dictobject.c:248`).
- **Read (plain):** `object.__getstate__()` — used by `pickle`, `copy`, and `shelve` — calls `object_getstate_default` → `_PyObject_IsInstanceDictEmpty(obj)`, which loops `for (i = 0; i < keys->dk_nentries; i++)` (`Objects/dictobject.c:7666`) with a **non-atomic** read.

Because the shared keys belong to the *type*, the two threads don't even need the same instance — same class + a genuinely new attribute name is enough. The race is value-benign (single aligned word, monotonically increasing, per-slot value reads are already atomic), but it is a real TSan-reported data race on operations (`pickle.dumps` / `copy.copy` / `obj.__getstate__()`) that callers treat as read-only.

## Reproducer

```python
import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

NR = 3                                  # reader threads: getstate (plain read of dk_nentries)
NW = 3                                  # writer threads: setattr new keys (atomic write)
ROUNDS = 3000
NAMES = ["a%d" % i for i in range(20)]  # < SHARED_KEYS_MAX_SIZE (30): stays split/inline-values

box = [None]
enter = threading.Barrier(NR + NW + 1)
leave = threading.Barrier(NR + NW + 1)

def reader():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        for _ in range(40):
            obj.__getstate__()          # -> object_getstate_default -> _PyObject_IsInstanceDictEmpty
        leave.wait()

def writer():
    for _ in range(ROUNDS):
        enter.wait()
        obj = box[0]
        for n in NAMES:
            setattr(obj, n, 1)          # first touch of a new name -> split_keys_entry_added
        leave.wait()

threads = ([threading.Thread(target=reader) for _ in range(NR)] +
           [threading.Thread(target=writer) for _ in range(NW)])
for t in threads:
    t.start()

for r in range(ROUNDS):
    C = type("C%d" % r, (), {})         # fresh type => fresh shared keys (dk_nentries grows from 0)
    box[0] = C()
    enter.wait()
    leave.wait()
for t in threads:
    t.join()
print("done, no crash")
```

The `shelve` fleet vehicle that seeded this did essentially the same thing: it hammered `pickle.dumps` on an object while a thread ran `setattr(_obj, "_tsan_a%d" % (_i % 4), _i)`. Using a **fresh type each round** (so `dk_nentries` starts at 0 and grows during the round) makes the first-touch insertion window hit reliably.

Run (free-threaded + TSan build):

```sh
DEBUGINFOD_URLS= PYTHON_GIL=0 TSAN_OPTIONS="halt_on_error=1:symbolize=1:history_size=4" \
  setarch -R ./python repro.py
```

## TSan report (confirmed, CPython 3.16.0a0 `--disable-gil --with-thread-sanitizer`, `heads/main:bcf98ddbc40`)

```
WARNING: ThreadSanitizer: data race (pid=2150454)
  Atomic write of size 8 at 0x7fffb6867e28 by thread T5:
    #0 _Py_atomic_store_ssize_relaxed  Include/cpython/pyatomic_gcc.h:513:3
    #1 split_keys_entry_added          Objects/dictobject.c:248:5
    #2 insert_split_key                Objects/dictobject.c:1940:9
    #3 store_instance_attr_lock_held   Objects/dictobject.c:7396:14
    #4 _PyObject_StoreInstanceAttribute Objects/dictobject.c:7528:19
    #5 _PyObject_GenericSetAttrWithDict Objects/object.c:2058:19
    ... PyObject_SetAttr -> builtin_setattr (setattr(obj, name, value))

  Previous read of size 8 at 0x7fffb6867e28 by thread T2:
    #0 _PyObject_IsInstanceDictEmpty   Objects/dictobject.c        (loop bound :7666)
    #1 object_getstate_default         Objects/typeobject.c:7928:9
    #2 object___getstate___impl        Objects/typeobject.c:8079:12
    ... object.__getstate__()   (also reached via pickle reduce_newobj / copy)

SUMMARY: ThreadSanitizer: data race Include/cpython/pyatomic_gcc.h:513:3 in _Py_atomic_store_ssize_relaxed
```

Reproduces deterministically (exit 66) within a few seconds, no crash. Across runs TSan may print the `SUMMARY` in terms of either side (`_Py_atomic_store_ssize_relaxed` or `_PyObject_IsInstanceDictEmpty`) — it is the same read/write pair on the same word. The confirmed signature matches the seeded one (same two racing functions; the seed reached the read via `pickle.dumps`→`reduce_newobj`→`object_getstate`, this repro reaches it via the direct `object.__getstate__()` C method, which is the identical `object_getstate_default` path).

## Root cause

`Objects/dictobject.c`, the writer, is deliberately atomic and even documents the read-race it is guarding against:

```c
static inline void split_keys_entry_added(PyDictKeysObject *keys)
{
    ASSERT_KEYS_LOCKED(keys);
    // We increase before we decrease so we never get too small of a value
    // when we're racing with reads
    _Py_atomic_store_ssize_relaxed(&keys->dk_nentries, keys->dk_nentries + 1);   // :248
    _Py_atomic_store_ssize_release(&keys->dk_usable, keys->dk_usable - 1);
}
```

The reader, in the same file, reads that field with a plain load:

```c
_PyObject_IsInstanceDictEmpty(PyObject *obj)
{
    ...
    if (tp->tp_flags & Py_TPFLAGS_INLINE_VALUES) {
        PyDictValues *values = _PyObject_InlineValues(obj);
        if (FT_ATOMIC_LOAD_UINT8(values->valid)) {
            PyDictKeysObject *keys = CACHED_KEYS(tp);
            for (Py_ssize_t i = 0; i < keys->dk_nentries; i++) {          // :7666  PLAIN read (racy)
                if (FT_ATOMIC_LOAD_PTR_RELAXED(values->values[i]) != NULL) { // per-slot read already atomic
                    return 0;
                }
            }
            return 1;
        }
        ...
```

The file already defines the correct accessor for exactly this field and uses it elsewhere:

```c
#define LOAD_KEYS_NENTRIES(keys) _Py_atomic_load_ssize_relaxed(&keys->dk_nentries)   // :237
...
for (i = 0; i < LOAD_KEYS_NENTRIES(a->ma_keys); i++) {   // dict_equal, :4632 — the correct idiom
```

So this is a plain missed-atomic-load: the loop bound at `:7666` should go through `LOAD_KEYS_NENTRIES`. `dk_nentries` on shared keys is monotonically increasing and the write side already orders `dk_usable` (release) after it, so a **relaxed** load is sufficient — the read may observe a value one step stale or ahead, but the inline `values` array is capacity-sized to the shared keys and each `values->values[i]` read is already `FT_ATOMIC_LOAD_PTR_RELAXED`, so there is no out-of-bounds and no torn pointer.

## Impact / severity

**Low.** Value-benign, crash-free data race:

- `dk_nentries` is a single aligned `Py_ssize_t`; the store is a single word and the value only grows, so the reader can only read a slightly stale/ahead count.
- The subsequent per-slot access uses an atomic load and stays within the capacity-sized inline `values` array, so there is no use-after-free or OOB.

But it is a genuine, easily-triggered TSan data race on `pickle.dumps` / `copy.copy` / `obj.__getstate__()` — operations user code reasonably treats as read-only and thread-safe on a shared object. Under `-fsanitize=thread` (or a future stricter memory model) it is undefined behavior and pollutes TSan runs of any code that pickles/copies objects concurrently with attribute mutation.

## Real bug vs. expected

**Real CPython free-threading bug**, in scope. This is not "don't share an object across threads": the write side (`split_keys_entry_added`) was *specifically* made atomic with a comment about racing with reads, and reading an object's state (pickle/copy) concurrently with `setattr` is a normal method race (unlike concurrent `__init__`/construction, cf. cpython#127192, which is out of scope). The reader in `_PyObject_IsInstanceDictEmpty` simply omitted the matching `LOAD_KEYS_NENTRIES`. Worth filing.

## Suggested fix

Use the existing relaxed-atomic accessor for the loop bound in `_PyObject_IsInstanceDictEmpty`:

```c
    PyDictKeysObject *keys = CACHED_KEYS(tp);
    for (Py_ssize_t i = 0; i < LOAD_KEYS_NENTRIES(keys); i++) {   // was: keys->dk_nentries
        if (FT_ATOMIC_LOAD_PTR_RELAXED(values->values[i]) != NULL) {
            return 0;
        }
    }
```

(`LOAD_KEYS_NENTRIES` is `_Py_atomic_load_ssize_relaxed(&keys->dk_nentries)` under `Py_GIL_DISABLED` and a plain read on the default build, so the non-free-threaded build is unaffected.)

## Notes

Found by ThreadSanitizer fuzzing (`fusil --tsan`); vehicle module `shelve`. The same shared-keys `dk_nentries` field is read in several places; any that iterate it with a plain `keys->dk_nentries` (rather than `LOAD_KEYS_NENTRIES`) while a concurrent `setattr` can add new keys are candidates for the same race and should be audited (e.g. other inline-values / split-dict iteration helpers). Resembles the known free-threading class of "writer made atomic, one reader missed the matching atomic load" rather than a distinct algorithmic bug.

---

*Part of an upcoming umbrella issue tracking free-threading data races found by `fusil --tsan`. Not yet individually filed.*
