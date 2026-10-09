"""Score an old overlay against today's content.

For each entry in OLD overlay, applied to today's stripped content:
  correct  : applies, and today's own overlay has the identical entry
  stale    : applies, but today's content no longer carries that markup there
  orphaned : its anchor is gone (the tested block or anchor line changed)
For each entry in TODAY's overlay: covered by old, or new since.
Pass a path prefix to restrict to a section.
"""
import json, sys
sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
from overlay import apply
from pathlib import Path

old = json.loads(Path(sys.argv[1]).read_text()); new = json.loads(Path(sys.argv[2]).read_text())
stripped_new = Path(sys.argv[3]); prefixes = sys.argv[4:] or [""]

def keys(rec):
    ks = set()
    for t in rec["tags"]: ks.add(("tag", t["fence"], t["occ"], json.dumps(t["form"], sort_keys=True)))
    for i in rec["inserts"]: ks.add(("ins", json.dumps(i["anchor"], sort_keys=True), "\n".join(i["raw"])))
    if rec.get("front_matter_test"): ks.add(("fm", "\n".join(rec["front_matter_test"]["raw"])))
    return ks

def inscope(f): return any(f.startswith(p) or ("/" + p) in f for p in prefixes)
c = dict(correct=0, stale=0, orphaned=0, file_gone=0, new_entries=0, covered=0)
for f, rec in old.items():
    if not inscope(f): continue
    n = len(keys(rec))
    p = stripped_new / f
    if not p.exists(): c["file_gone"] += n; continue
    _, orphans = apply(p.read_text(), rec)
    nk = keys(new.get(f, {"tags": [], "inserts": [], "front_matter_test": None}))
    ok = keys(rec) & nk
    c["correct"] += len(ok); c["orphaned"] += len(orphans); c["stale"] += max(0, n - len(ok) - len(orphans))
for f, rec in new.items():
    if not inscope(f): continue
    nk = keys(rec); ok = keys(old.get(f, {"tags": [], "inserts": [], "front_matter_test": None}))
    c["covered"] += len(nk & ok); c["new_entries"] += len(nk - ok)
tot = c["correct"] + c["stale"] + c["orphaned"] + c["file_gone"]
print(f"old entries {tot}: correct {c['correct']} ({100*c['correct']/max(tot,1):.1f}%), orphaned {c['orphaned']}, file gone {c['file_gone']}, stale/moved {c['stale']}")
print(f"today's entries {c['covered']+c['new_entries']}: already in old overlay {c['covered']}, added since {c['new_entries']}")
