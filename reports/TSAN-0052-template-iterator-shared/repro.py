import threading

# A shared t-string TemplateIter: templateiter_next writes self->from_strings and advances the
# shared strings/interpolations sub-iterators non-atomically -> data race when shared across threads.
NT = 8


def worker(it, barrier):
    barrier.wait()
    try:
        for _ in it:
            pass
    except (StopIteration, RuntimeError, ValueError):
        pass


a = b = 1
for _round in range(6000):
    shared = iter(t"x{a}y{b}z")  # ONE shared TemplateIter
    bar = threading.Barrier(NT)
    ts = [threading.Thread(target=worker, args=(shared, bar)) for _ in range(NT)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
print("done")
