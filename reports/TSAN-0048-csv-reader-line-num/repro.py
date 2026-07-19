import csv
import io
import threading

# A shared csv.reader iterator: one thread advances it (Reader_iternext writes self->line_num,
# Modules/_csv.c) while another reads reader.line_num (a T_ULONG member) -> data race on line_num.
NT = 8
barrier = threading.Barrier(NT)


def worker(rdr, role):
    barrier.wait()
    for _ in range(5000):
        if role:
            try:
                next(rdr)
            except StopIteration:
                pass
        else:
            _ = rdr.line_num


for _round in range(300):
    rdr = csv.reader(io.StringIO("a,b\n" * 500))
    ts = [threading.Thread(target=worker, args=(rdr, i % 2)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
