#!/usr/bin/env python3
"""Close matching for doc-test annotations, plus a backtest against git history.

Annotation additions over overlay.py (all hashes, no readable text):
  blocks: ordered list of every fence in the file: {h, lang, lines: [line hashes]}
  tag / fence-anchored insert: "bi" = index into blocks
  line-anchored insert: anchor line hash + occ, prev_bi/next_bi (fences around it),
                        words: hashes of the anchor line's words

Matching a file's old annotations to its new text:
  1. Align old block hashes to new block hashes (difflib opcodes).
     equal  -> EXACT (the block is unchanged and in the same order)
     an old block whose hash appears exactly once among the new UNALIGNED blocks -> EXACT (moved)
  2. Otherwise the block sits in a `replace` region. Candidates are the new blocks in
     that region with the same language. Score = Jaccard of line-hash sets.
     Best >= T and best - second >= M  -> CLOSE.  Else UNMATCHED.
  Line-anchored inserts: exact line between the mapped neighbor fences, else the best
  word-Jaccard line in that window (same T / M rule), else UNMATCHED.
"""
import difflib
import hashlib
import re
import textwrap

from overlay import FENCE, scan, split_front_matter, strip_paths


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def norm_lines(content_lines):
    body = textwrap.dedent("\n".join(l.rstrip() for l in content_lines)).strip()
    return body, [h(x.strip()) for x in body.split("\n") if x.strip()]


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else 1.0


def words(line):
    return [h(w) for w in re.findall(r"[A-Za-z0-9_.-]+", line.lower())]


def parse(body):
    """Stripped body -> (lines, blocks, prose) with tags/inserts recorded relative to them."""
    lines = body.split("\n")
    out, blocks, tags, inserts, prose = [], [], [], [], []
    last_bi = -1

    def anchor():
        for k in range(len(out) - 1, -1, -1):
            kind = out[k][1]
            if kind == "blank":
                continue
            if kind[0] == "close":
                return {"bi": kind[1]}
            if kind[0] == "line":
                return {"line": kind[1], "occ": kind[2], "prev_bi": kind[3], "words": kind[4]}
            if kind[0] == "in":
                continue
        return {"start": True}

    line_occ = {}
    for tok in scan(lines):
        if tok[0] == "dt":
            _, i, j = tok
            bb = []
            while out and out[-1][1] == "blank":
                bb.insert(0, out.pop()[0])
            a = anchor()
            inserts.append({"anchor": a, "raw": lines[i:j + 1], "next_bi": len(blocks)})
        elif tok[0] == "fence":
            _, i, j, m = tok
            content = lines[i + 1:j]
            _, lh = norm_lines(content)
            info = m.group(3)
            new_info, form = strip_paths(info)
            lang = (re.split(r"[,{\s]", new_info.strip(), maxsplit=1)[0] if new_info.strip() else "").lower()
            bi = len(blocks)
            blocks.append({"h": h("\n".join(lh)), "lang": lang, "lines": lh})
            if form:
                tags.append({"bi": bi, "attr": form.get("attr") or form.get("bare")})
            out.append((m.group(1) + m.group(2) + new_info, ("in",)))
            for c in content:
                out.append((c, ("in",)))
            out.append((lines[j] if j < len(lines) else "", ("close", bi)))
            last_bi = bi
        else:
            l = lines[tok[1]]
            if not l.strip():
                out.append((l, "blank"))
            else:
                lh = h(l.strip()); occ = line_occ.get(lh, 0); line_occ[lh] = occ + 1
                out.append((l, ("line", lh, occ, last_bi, words(l))))
                prose.append({"idx": len(out) - 1, "h": lh, "occ": occ, "prev_bi": last_bi, "words": words(l)})
    return {"blocks": blocks, "tags": tags, "inserts": inserts, "prose": prose, "n_out": len(out)}


def parse_text(text):
    _, body = split_front_matter(text)
    return parse(body)


def align(old_blocks, new_blocks):
    """old index -> ('exact', new j) | ('region', [candidate js]) | ('gone', [])"""
    oh = [b["h"] for b in old_blocks]
    nh = [b["h"] for b in new_blocks]
    res, aligned_new = {}, set()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, oh, nh, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                res[i1 + k] = ("exact", j1 + k); aligned_new.add(j1 + k)
        elif op == "replace":
            for i in range(i1, i2):
                res[i] = ("region", list(range(j1, j2)))
        elif op == "delete":
            for i in range(i1, i2):
                res[i] = ("gone", [])
    unaligned = {}
    for j, b in enumerate(new_blocks):
        if j not in aligned_new:
            unaligned.setdefault(b["h"], []).append(j)
    for i, r in list(res.items()):
        if r[0] != "exact":
            js = unaligned.get(oh[i], [])
            if len(js) == 1:
                res[i] = ("exact", js[0])  # moved, unchanged
    return res


def score_block(old_b, new_blocks, cands):
    scored = sorted(
        ((jaccard(old_b["lines"], new_blocks[j]["lines"]), j) for j in cands if new_blocks[j]["lang"] == old_b["lang"]),
        reverse=True,
    )
    best = scored[0] if scored else (0.0, None)
    second = scored[1][0] if len(scored) > 1 else 0.0
    return best[1], best[0], second
