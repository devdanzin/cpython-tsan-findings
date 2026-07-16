import sys, threading
assert not sys._is_gil_enabled(), "run free-threaded: PYTHON_GIL=0"

# sys.monitoring.use_tool_id(tool_id, name) does an unsynchronized check-then-act on the
# interpreter-global registry interp->monitoring_tool_names[tool_id]:
#     if (interp->monitoring_tool_names[tool_id] != NULL) { raise; }   # :2190  read
#     interp->monitoring_tool_names[tool_id] = Py_NewRef(name);        # :2194  write
# Many threads racing to claim the SAME free tool id read the slot while one writes it.
mon = sys.monitoring
TOOL_ID = 3                 # any 0..5; not reserved
NT = 8
ROUNDS = 6000

enter = threading.Barrier(NT + 1)
leave = threading.Barrier(NT + 1)

def worker():
    for _ in range(ROUNDS):
        enter.wait()            # all workers released together onto a freshly-freed slot
        try:
            mon.use_tool_id(TOOL_ID, "t")   # read :2190 races the winner's write :2194
        except ValueError:
            pass                # "tool 3 is already in use" -> lost the race, expected
        leave.wait()

ts = [threading.Thread(target=worker, name=f"w{i}") for i in range(NT)]
for t in ts:
    t.start()
for r in range(ROUNDS):
    mon.free_tool_id(TOOL_ID)   # NULL the slot so the next round starts from a free id
    enter.wait()
    leave.wait()
for t in ts:
    t.join()
print("done, no crash")
