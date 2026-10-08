"""dump_sources.py — يجلب المصادر ويحفظها في /tmp/sources.json للتحليل."""
import json
import time

from run_all import _fetch

out = []
for src in ["u3seq", "yam"]:
    t = time.time()
    items = _fetch(src)
    for s in items:
        s["_src"] = src
    print(f"[{src}] {len(items)} in {time.time()-t:.1f}s", flush=True)
    out.extend(items)

with open("/tmp/sources.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"saved {len(out)} to /tmp/sources.json")
