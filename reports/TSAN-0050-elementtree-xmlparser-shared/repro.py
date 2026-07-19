import threading
import xml.etree.ElementTree as ET

# A shared _elementtree.XMLParser wrapping one expat parser: concurrent feed()/flush()/close()
# race the expat parser's internal state (XML_Parse / SetReparseDeferralEnabled / callProcessor).
# expat is single-threaded by design; XMLParser takes no lock around the expat calls.
NT = 8
barrier = threading.Barrier(NT)


def worker(parser, role):
    barrier.wait()
    for _ in range(500):
        try:
            if role == 0:
                parser.feed(b"<a>x</a>")
            elif role == 1:
                parser.flush()
            else:
                parser.close()
        except Exception:
            pass


for _round in range(400):
    parser = ET.XMLParser()
    ts = [threading.Thread(target=worker, args=(parser, i % 3)) for i in range(NT)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
print("done")
