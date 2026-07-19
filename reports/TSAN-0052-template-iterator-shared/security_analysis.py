import threading

# Security analysis of the templateiter race (TSAN-0052). PEP 750 t-strings let a renderer trust
# STATIC strings and escape INTERPOLATIONS (untrusted {expr} values) by iterating the template.
# This checks what concurrent iteration of ONE shared TemplateIter does to that sequence -- and,
# crucially, whether it can turn an untrusted interpolation into trusted static (an injection).


def make_template():
    u0, u1, u2, u3, u4, u5 = ("U0", "U1", "U2", "U3", "U4", "U5")
    return t"S0{u0}S1{u1}S2{u2}S3{u3}S4{u4}S5{u5}S6"


ref = [it if isinstance(it, str) else ("INTERP", it.value) for it in make_template()]


def render(seq):
    out = []
    for it in seq:
        if isinstance(it, str):
            out.append(it)  # TRUSTED static, verbatim
        else:
            out.append("[esc:%s]" % it.value)  # UNTRUSTED interpolation, escaped
    return "".join(out)


print("reference render:", render(make_template()))

NT = 12
corrupted = 0
multiset_changed = 0
type_confused = 0
examples = []
lock = threading.Lock()


def worker(it, collected, barrier):
    barrier.wait()
    while True:
        try:
            item = next(it)
        except StopIteration:
            break
        with lock:
            collected.append(item)


for _round in range(20000):
    shared = iter(make_template())
    collected = []
    barrier = threading.Barrier(NT)
    ts = [threading.Thread(target=worker, args=(shared, collected, barrier)) for _ in range(NT)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    got = [x if isinstance(x, str) else ("INTERP", x.value) for x in collected]
    # a value that was an interpolation appearing as a plain str (or vice versa) = trust confusion
    ref_vals = {v for _, v in [g for g in ref if not isinstance(g, str)]}
    for x in collected:
        if isinstance(x, str) and x in ref_vals:
            type_confused += 1
    if got != ref:
        corrupted += 1
        if sorted(map(str, got)) != sorted(map(str, ref)):
            multiset_changed += 1
        if len(examples) < 3:
            examples.append(render(collected))
    if corrupted and _round > 400 and len(examples) >= 3:
        break

print("corrupted (reordered) iterations:", corrupted)
print("iterations with items LOST/DUPLICATED (multiset changed):", multiset_changed)
print("TRUST CONFUSION (interpolation value seen as static str):", type_confused, "<-- 0 = no injection")
for r in examples:
    print("  rendered:", r)
