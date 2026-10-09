#!/usr/bin/env python3
"""Replay agentgateway/website history to measure close matching.

Ground truth: the repo still carries its markup inline, so for every commit that
changed a page, the AFTER version shows where the author actually left each tag
and hidden check. We build annotations from the BEFORE version, match them to the
AFTER version with markup stripped, and compare.

Usage: backtest.py <repo> <since> <out.json>
"""
import json
import subprocess
import sys

from closematch import align, jaccard, parse_text, score_block

repo, since, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

log = subprocess.run(
    ["git", "-C", repo, "log", "--no-merges", f"--since={since}", "--diff-filter=MR", "-M",
     "--name-status", "--format=@%H %P", "origin/main", "--", "assets/**/*.md", "content/**/*.md"],
    capture_output=True, text=True, check=True).stdout
pairs = []
cur = None
for line in log.splitlines():
    if line.startswith("@"):
        parts = line[1:].split()
        cur = (parts[0], parts[1] if len(parts) > 1 else None)
    elif line.strip() and cur and cur[1]:
        f = line.split("\t")
        if f[0].startswith("R"):
            pairs.append((cur[1], f[1], cur[0], f[2]))
        elif f[0] == "M":
            pairs.append((cur[1], f[1], cur[0], f[1]))

cat = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def blob(ref, path):
    cat.stdin.write(f"{ref}:{path}\n".encode()); cat.stdin.flush()
    header = cat.stdout.readline().decode()
    if header.endswith("missing\n"):
        return None
    size = int(header.split()[2])
    data = cat.stdout.read(size); cat.stdout.read(1)
    return data.decode("utf-8", "replace")


rows = []
stats = {"pairs": len(pairs), "with_markup": 0, "with_changed_tested_blocks": 0}
for parent, opath, commit, npath in pairs:
    old_t = blob(parent, opath)
    if not old_t or ("paths=" not in old_t and "doc-test" not in old_t):
        continue
    new_t = blob(commit, npath)
    if new_t is None:
        continue
    stats["with_markup"] += 1
    old, new = parse_text(old_t), parse_text(new_t)
    if not old["tags"] and not old["inserts"]:
        continue
    m = align(old["blocks"], new["blocks"])
    scope = "traffic-management" if "traffic-management/" in npath else "other"
    ctx = {"file": npath, "commit": commit[:8], "scope": scope}

    truth = {}
    for t in new["tags"]:
        truth.setdefault(t["attr"], set()).add(t["bi"])
    claimed = {}
    for t in old["tags"]:
        r = m.get(t["bi"], ("gone", []))
        if r[0] == "exact":
            claimed.setdefault(t["attr"], set()).add(r[1])
    changed = False
    for t in old["tags"]:
        r = m.get(t["bi"], ("gone", []))
        tset = truth.get(t["attr"], set())
        if r[0] == "exact":
            rows.append({**ctx, "kind": "tag", "exact": True, "correct": r[1] in tset, "truth_exists": bool(tset)})
            continue
        changed = True
        remaining = tset - claimed.get(t["attr"], set())
        best, score, second = (None, 0.0, 0.0)
        if r[0] == "region":
            best, score, second = score_block(old["blocks"][t["bi"]], new["blocks"], r[1])
        rows.append({**ctx, "kind": "tag", "exact": False, "region": r[0], "n_cands": len(r[1]),
                     "best": best, "score": score, "second": second,
                     "best_correct": best in remaining if best is not None else False,
                     "truth_exists": bool(remaining)})

    # hidden checks: find each old insert's truth anchor in the new file (by raw text, in order)
    pool = {}
    for ins in new["inserts"]:
        pool.setdefault("\n".join(ins["raw"]), []).append(ins["anchor"])
    for ins in old["inserts"]:
        a = ins["anchor"]
        lst = pool.get("\n".join(ins["raw"]))
        truth_a = lst.pop(0) if lst else None
        if "start" in a:
            rows.append({**ctx, "kind": "ins_start", "exact": True, "correct": truth_a is not None and "start" in truth_a, "truth_exists": truth_a is not None})
            continue
        if "bi" in a:
            r = m.get(a["bi"], ("gone", []))
            if r[0] == "exact":
                rows.append({**ctx, "kind": "ins_fence", "exact": True,
                             "correct": truth_a is not None and truth_a.get("bi") == r[1], "truth_exists": truth_a is not None})
                continue
            changed = True
            best, score, second = (None, 0.0, 0.0)
            if r[0] == "region":
                best, score, second = score_block(old["blocks"][a["bi"]], new["blocks"], r[1])
            rows.append({**ctx, "kind": "ins_fence", "exact": False, "region": r[0], "best": best, "score": score, "second": second,
                         "best_correct": truth_a is not None and best is not None and truth_a.get("bi") == best,
                         "truth_exists": truth_a is not None})
            continue
        # line anchor: candidates are prose lines after the same (mapped) fence
        pb = a["prev_bi"]
        if pb == -1:
            window_prev = -1
        else:
            r = m.get(pb, ("gone", []))
            window_prev = r[1] if r[0] == "exact" else None
        cands = [p for p in new["prose"] if window_prev is not None and p["prev_bi"] == window_prev]
        exact = [p for p in cands if p["h"] == a["line"]]
        if exact:
            p = exact[0]
            rows.append({**ctx, "kind": "ins_line", "exact": True,
                         "correct": truth_a is not None and truth_a.get("line") == p["h"] and truth_a.get("occ") == p["occ"],
                         "truth_exists": truth_a is not None})
            continue
        changed = True
        scored = sorted(((jaccard(a["words"], p["words"]), k) for k, p in enumerate(cands)), reverse=True)
        best = cands[scored[0][1]] if scored else None
        score = scored[0][0] if scored else 0.0
        second = scored[1][0] if len(scored) > 1 else 0.0
        rows.append({**ctx, "kind": "ins_line", "exact": False, "region": "window" if window_prev is not None else "neighbor_gone",
                     "score": score, "second": second,
                     "best_correct": bool(truth_a and best and truth_a.get("line") == best["h"] and truth_a.get("occ") == best["occ"]),
                     "truth_exists": truth_a is not None})
    if changed:
        stats["with_changed_tested_blocks"] += 1

json.dump({"stats": stats, "rows": rows}, open(out_path, "w"))
print(stats, "rows:", len(rows))
