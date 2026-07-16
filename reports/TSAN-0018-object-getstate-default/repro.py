import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# object.__getstate__()  (object_getstate_default, used by pickle/copy/shelve) calls
# _PyObject_IsInstanceDictEmpty(obj), which reads the type's shared-keys dk_nentries as a
# PLAIN (non-atomic) loop bound:  for (i = 0; i < keys->dk_nentries; i++).
# Meanwhile setattr(obj, NEW_NAME, v) inserts a key into those same shared keys and bumps
# dk_nentries via split_keys_entry_added() -> _Py_atomic_store_ssize_relaxed (an ATOMIC store).
# Reader (plain load) vs writer (atomic store) on the same word => TSan data race.
#
# The shared keys belong to the TYPE, so a fresh class each round gives fresh keys whose
# dk_nentries starts at 0 and grows as new attribute names are first inserted -- that first
# insertion is the racing write window.

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
    enter.wait()                        # release workers onto the fresh object
    leave.wait()                        # wait for them to finish this round
for t in threads:
    t.join()
print("done, no crash")
