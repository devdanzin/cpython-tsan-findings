import threading
import xml.etree.ElementTree as ET
# one shared Element, hammered: clear() (teardown) vs len()/.tail read vs subelement churn
def mk():
    e = ET.Element("root")
    for i in range(8):
        c = ET.SubElement(e, "c%d" % i); c.text="t"; c.tail="x"
    return e
el = mk()
NT=8; ITERS=40000
def worker(w):
    for i in range(ITERS):
        try:
            if w % 2 == 0:
                el.clear()
                for j in range(4): ET.SubElement(el, "s%d" % j).tail="y"
            else:
                _ = len(el)
                for ch in list(el): _ = ch.tail
        except Exception: pass
ts=[threading.Thread(target=worker,args=(w,)) for w in range(NT)]
for t in ts: t.start()
for t in ts: t.join()
print("done-et")
