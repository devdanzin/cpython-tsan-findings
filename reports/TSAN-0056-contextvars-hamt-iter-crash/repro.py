# shared hamt iterator over a Context, advanced by many threads (op-h pattern)
import contextvars, threading
vs = [contextvars.ContextVar(f"v{i}") for i in range(16)]
ctx = contextvars.copy_context()
def populate():
    for i, v in enumerate(vs): v.set(i)
ctx.run(populate)
cell = [iter(ctx)]
N = 40000
def worker():
    for _ in range(N):
        it = cell[0]
        try:
            next(it)
        except StopIteration:
            cell[0] = iter(ctx)
        except Exception:
            pass
ts = [threading.Thread(target=worker) for _ in range(8)]
for t in ts: t.start()
for t in ts: t.join()
print("done-cv2")
