import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# Reader-biased variant: more clear() threads so ThreadSanitizer is more likely
# to catch clear_lock_held as the "current" access (matching the vehicle SUMMARY).
NT = 8
NWRITERS = 2          # grow shared keys; rest clear instance dicts
ROUNDS = 400
NINST = 24
NNAMES = 25           # < SHARED_KEYS_MAX_SIZE (30) so dicts stay split
box = [None]
enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker(wid):
    grow = (wid < NWRITERS)
    for _ in range(ROUNDS):
        enter.wait()
        insts, names = box[0]
        if grow:
            for inst in insts:
                for nm in names:
                    try:
                        setattr(inst, nm, 1)   # insert_split_key -> split_keys_entry_added (atomic write dk_nentries)
                    except Exception:
                        pass
        else:
            for inst in insts:
                try:
                    inst.__dict__.clear()      # clear_lock_held reads dk_nentries (plain)
                except Exception:
                    pass
        leave.wait()

ts = [threading.Thread(target=worker, args=(i,)) for i in range(NT)]
for t in ts:
    t.start()

for r in range(ROUNDS):
    C = type(f"C{r}", (), {})                  # fresh class -> fresh empty shared keys
    insts = [C() for _ in range(NINST)]
    names = [f"a{r}_{k}" for k in range(NNAMES)]
    box[0] = (insts, names)
    enter.wait()
    leave.wait()

for t in ts:
    t.join()
print("done, no crash")
