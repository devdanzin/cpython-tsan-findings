# shared Pickler: dump() (reads memo) races clear_memo() (PyMemoTable_Clear)
import _pickle, io, threading
p = _pickle.Pickler(io.BytesIO())
N = 30000
def dumper():
    for _ in range(N):
        try: p.dump([1, 2, 3, "x", {"k": 1}])
        except Exception: pass
def clearer():
    for _ in range(N):
        try: p.clear_memo()
        except Exception: pass
ts = [threading.Thread(target=dumper) for _ in range(4)] + \
     [threading.Thread(target=clearer) for _ in range(4)]
for t in ts: t.start()
for t in ts: t.join()
print("done-pickle")
