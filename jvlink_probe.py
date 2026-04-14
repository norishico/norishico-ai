"""Step A diagnostic - unbuffered, step-by-step flush, no JVCancel."""
import sys
import win32com.client
from collections import Counter


def p(msg):
    print(msg, flush=True)


p("start")
jv = win32com.client.Dispatch("JVDTLab.JVLink")
p("dispatch ok")

rc = jv.JVInit("NORISHIKO/1.0.0.0")
p(f"JVInit rc={rc}")

try:
    jv.JVClose()
    p("JVClose pre ok")
except Exception as e:
    p(f"JVClose pre err: {e}")

result = jv.JVOpen("RACE", "20260320000000", 1)
p(f"JVOpen: {result}")

BUF = 110000
counts = Counter()
file_first_seen = {}
total = 0
MAX = 20000
prev_file = None
while True:
    r = jv.JVRead(" " * BUF, BUF, " " * 256)
    size, buf, _, filename = r
    if size == 0:
        p(f"all files done: size=0")
        break
    if size == -1:
        # End of current file - next call will return the first record of next file
        if total < 600 or total % 1000 == 0:
            p(f"  EOF for file at total={total}")
        continue
    if size == -3:
        continue
    if size < 0:
        p(f"error size={size}")
        break
    if filename != prev_file:
        prev = filename[:2]
        file_first_seen[filename] = total
        p(f"  --> file #{len(file_first_seen)}: {filename} (prefix={prev}) at total={total}")
        prev_file = filename
    counts[buf[:2]] += 1
    total += 1
    if total % 500 == 0:
        p(f"  progress: total={total} files={len(file_first_seen)}")
    if total >= MAX:
        p("cap reached")
        break

p(f"total={total} files={len(file_first_seen)}")
for rt, n in sorted(counts.items(), key=lambda x: -x[1]):
    p(f"  {rt}: {n}")

try:
    jv.JVClose()
    p("JVClose post ok")
except Exception as e:
    p(f"JVClose post err: {e}")
p("done")
