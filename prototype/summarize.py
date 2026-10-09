#!/usr/bin/env python3
"""Summarize backtest rows at several thresholds. Usage: summarize.py <rows.json> [scope]"""
import json
import sys
from collections import Counter

d = json.load(open(sys.argv[1]))
scope = sys.argv[2] if len(sys.argv) > 2 else None
rows = [r for r in d["rows"] if scope is None or r["scope"] == scope]
print(d["stats"], "| scope:", scope or "all", "| annotation instances:", len(rows))

ex = [r for r in rows if r["exact"]]
c = Counter((r["correct"], r["truth_exists"]) for r in ex)
print(f"EXACT: {len(ex)}  right place {c[(True, True)]}  author moved it {c[(False, True)]}  author removed it {c[(False, False)]}")

ne = [r for r in rows if not r["exact"]]
print(f"NOT EXACT (tested block changed): {len(ne)}  by kind {dict(Counter(r['kind'] for r in ne))}")
print(f"  of these, author kept the markup somewhere: {sum(r['truth_exists'] for r in ne)}; removed it: {sum(not r['truth_exists'] for r in ne)}")
print(f"{'T':>5} {'margin':>6} | {'close right':>11} {'close WRONG':>11} {'on removed':>10} | {'missed':>6} {'right to skip':>13} | precision  recall")
for T in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
    for M in (0.0, 0.1):
        k = Counter()
        for r in ne:
            chosen = r.get("score", 0) >= T and r.get("score", 0) - r.get("second", 0) >= M and r.get("region") in ("region", "window")
            if chosen:
                k["right" if r["best_correct"] else ("wrong" if r["truth_exists"] else "on_removed")] += 1
            else:
                k["missed" if r["truth_exists"] else "skip_ok"] += 1
        prec = k["right"] / max(k["right"] + k["wrong"], 1)
        rec = k["right"] / max(sum(r["truth_exists"] for r in ne), 1)
        print(f"{T:>5} {M:>6} | {k['right']:>11} {k['wrong']:>11} {k['on_removed']:>10} | {k['missed']:>6} {k['skip_ok']:>13} | {prec:9.1%} {rec:7.1%}")
