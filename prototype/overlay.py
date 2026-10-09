#!/usr/bin/env python3
"""Prototype: keep doc-test markup OUT of content, as an overlay applied at test time.

Markup kinds handled:
  1. `paths="..."` inside a fence opener's {...} attributes
  2. hidden {{< doc-test ... >}} ... {{< /doc-test >}} blocks
  3. a top-level `test:` key in front matter

export(text) -> (stripped_text, entry)   entry holds only markup + anchors (hashes)
apply(stripped_text, entry) -> (text, orphans)

Anchors never store prose: a fence is identified by the hash of its normalized
content (+ occurrence index), a prose line by the hash of its stripped text.
"""
import hashlib
import json
import re
import sys
import textwrap
from pathlib import Path

FENCE = re.compile(r"^(\s*)(`{3,})(.*)$")
DT_OPEN = re.compile(r"\{\{[<%]\s*doc-test\b")
DT_CLOSE = re.compile(r"\{\{[<%]\s*/doc-test\s*[>%]\}\}")
PATHS_ATTR = re.compile(r'paths\s*=\s*"[^"]*"')


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def fence_hash(content_lines):
    return h(textwrap.dedent("\n".join(l.rstrip() for l in content_lines)).strip())


def split_front_matter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[: end + 5], text[end + 5 :]
        if text.rstrip("\n").endswith("\n---"):
            return text, ""
    return "", text


# ---------------------------------------------------------------- front matter
def strip_fm_test(fm):
    if not fm:
        return fm, None
    lines = fm.split("\n")
    for i, l in enumerate(lines):
        if i and re.match(r"^test:", l):
            j = i + 1
            while j < len(lines) and (lines[j] == "" or lines[j][:1] in (" ", "\t", "-")) and lines[j] != "---":
                j += 1
            prev = lines[i - 1]
            occ = lines[: i - 1].count(prev)
            return "\n".join(lines[:i] + lines[j:]), {"after_line": h(prev), "occ": occ, "raw": lines[i:j]}
    return fm, None


def apply_fm_test(fm, rec):
    if not rec:
        return fm, []
    lines = fm.split("\n")
    seen = 0
    for i, l in enumerate(lines):
        if h(l) == rec["after_line"]:
            if seen == rec["occ"]:
                return "\n".join(lines[: i + 1] + rec["raw"] + lines[i + 1 :]), []
            seen += 1
    return fm, ["front matter: line before test: not found"]


# ---------------------------------------------------------------- body
def scan(lines):
    """Yield ('fence', i, j, opener_match) / ('dt', i, j) / ('line', i) over a body."""
    i = 0
    while i < len(lines):
        l = lines[i]
        if DT_OPEN.search(l):
            j = i
            while j < len(lines) and not DT_CLOSE.search(lines[j]):
                j += 1
            yield ("dt", i, j)
            i = j + 1
            continue
        m = FENCE.match(l)
        if m:
            close = re.compile(rf"^\s*`{{{len(m.group(2))},}}\s*(\{{\{{[<%].*)?$")
            j = i + 1
            while j < len(lines) and not close.match(lines[j]):
                j += 1
            yield ("fence", i, j, m)
            i = j + 1
            continue
        yield ("line", i)
        i += 1


ATTR_TOKEN = re.compile(r'[\w-]+\s*=\s*"[^"]*"|[^\s,]+')


def strip_paths(info):
    """Remove paths="..." from an opener's info string; return (new_info, form).

    Forms: `{paths="x"}` alone, inside `{a=b,paths="x"}`, or bare `lang,paths="x"`.
    """
    m = re.search(r"(\s*)\{([^}]*)\}", info)
    if m and PATHS_ATTR.search(m.group(2)):
        inner = m.group(2)
        parts = ATTR_TOKEN.findall(inner)
        sep = "," if re.search(r'"\s*,|,\s*[\w-]+\s*=', inner) or ("," in inner and '"' not in inner) else " "
        pos = next(k for k, p in enumerate(parts) if PATHS_ATTR.fullmatch(p))
        attr = parts.pop(pos)
        if inner != sep.join(parts[:pos] + [attr] + parts[pos:]):
            return info, None  # unusual spacing: leave it, the round-trip check will report it
        if parts:
            new = info[: m.start(2)] + sep.join(parts) + info[m.end(2):]
        else:
            new = info[: m.start()] + info[m.end():]
        return new, {"attr": attr, "pos": pos, "sep": sep, "lead": m.group(1), "only": not parts, "tail": info[m.end():] if not parts else ""}
    b = re.search(r',\s*paths\s*=\s*"[^"]*"', info)
    if b:
        return info[: b.start()] + info[b.end():], {"bare": b.group(0), "at": b.start()}
    return info, None


def restore_paths(info, form):
    if "bare" in form:
        return info[: form["at"]] + form["bare"] + info[form["at"]:]
    if form["only"]:
        tail = form.get("tail", "")
        if tail and info.endswith(tail):
            return info[: len(info) - len(tail)] + form["lead"] + "{" + form["attr"] + "}" + tail
        return info + form["lead"] + "{" + form["attr"] + "}"
    m = re.search(r"\{([^}]*)\}", info)
    if not m:
        return None
    parts = ATTR_TOKEN.findall(m.group(1))
    parts.insert(min(form["pos"], len(parts)), form["attr"])
    return info[: m.start(1)] + form["sep"].join(parts) + info[m.end(1):]


def export_body(body):
    lines = body.split("\n")
    out = []            # stripped lines
    kinds = []          # parallel to out: ('fence_close', hash) / ('line', hash) / ('blank',) / ('in_fence',)
    tags, inserts = [], []
    fence_occ, line_occ = {}, {}

    def anchor():
        # nearest preceding non-blank stripped line
        for k in range(len(kinds) - 1, -1, -1):
            if kinds[k][0] == "blank":
                continue
            if kinds[k][0] == "fence_close":
                return {"fence": kinds[k][1], "occ": kinds[k][2]}
            if kinds[k][0] == "line":
                return {"line": kinds[k][1], "occ": kinds[k][2]}
        return {"start": True}

    for tok in scan(lines):
        if tok[0] == "dt":
            _, i, j = tok
            bb = []
            while out and kinds[-1][0] == "blank":
                bb.insert(0, out.pop()); kinds.pop()
            inserts.append({"anchor": anchor(), "blank_before": bb, "raw": lines[i : j + 1]})
        elif tok[0] == "fence":
            _, i, j, m = tok
            content = lines[i + 1 : j]
            fh = fence_hash(content)
            occ = fence_occ.get(fh, 0); fence_occ[fh] = occ + 1
            indent, fence, info = m.group(1), m.group(2), m.group(3)
            new_info, form = strip_paths(info)
            if form:
                tags.append({"fence": fh, "occ": occ, "form": form})
            out.append(indent + fence + new_info); kinds.append(("in_fence",))
            for c in content:
                out.append(c); kinds.append(("in_fence",))
            if j < len(lines):
                out.append(lines[j]); kinds.append(("fence_close", fh, occ))
        else:
            l = lines[tok[1]]
            if not l.strip():
                out.append(l); kinds.append(("blank",))
            else:
                lh = h(l.strip()); occ = line_occ.get(lh, 0); line_occ[lh] = occ + 1
                out.append(l); kinds.append(("line", lh, occ))
    return "\n".join(out), {"tags": tags, "inserts": inserts}


def apply_body(body, rec):
    lines = body.split("\n")
    orphans = []
    fences = {}       # (hash, occ) -> (open_idx, close_idx)
    line_at = {}      # (hash, occ) -> idx
    fence_occ, line_occ = {}, {}
    for tok in scan(lines):
        if tok[0] == "fence":
            _, i, j, m = tok
            fh = fence_hash(lines[i + 1 : j]); occ = fence_occ.get(fh, 0); fence_occ[fh] = occ + 1
            fences[(fh, occ)] = (i, j)
        elif tok[0] == "line" and lines[tok[1]].strip():
            lh = h(lines[tok[1]].strip()); occ = line_occ.get(lh, 0); line_occ[lh] = occ + 1
            line_at[(lh, occ)] = tok[1]
        elif tok[0] == "dt":
            orphans.append("source already contains a doc-test block")
    for t in rec["tags"]:
        loc = fences.get((t["fence"], t["occ"]))
        if loc is None:
            orphans.append(f"tag {t['form'].get('attr') or t['form'].get('bare')}: fence {t['fence']}#{t['occ']} not found")
            continue
        m = FENCE.match(lines[loc[0]])
        new = restore_paths(m.group(3), t["form"])
        if new is None:
            orphans.append(f"tag {t['form'].get('attr') or t['form'].get('bare')}: cannot restore attr form"); continue
        lines[loc[0]] = m.group(1) + m.group(2) + new
    after = {}  # idx -> list of inserts ; -1 = start
    for ins in rec["inserts"]:
        a = ins["anchor"]
        if "start" in a:
            idx = -1
        elif "fence" in a:
            loc = fences.get((a["fence"], a["occ"]))
            idx = loc[1] if loc else None
        else:
            idx = line_at.get((a["line"], a["occ"]))
        if idx is None:
            orphans.append(f"hidden block {ins['raw'][0].strip()}: anchor {a} not found")
            continue
        after.setdefault(idx, []).append(ins)
    out = []
    for ins in after.get(-1, []):
        out += ins["blank_before"] + ins["raw"]
    for i, l in enumerate(lines):
        out.append(l)
        for ins in after.get(i, []):
            out += ins["blank_before"] + ins["raw"]
    return "\n".join(out), orphans


def export(text):
    fm, body = split_front_matter(text)
    fm2, fmrec = strip_fm_test(fm)
    body2, rec = export_body(body)
    rec["front_matter_test"] = fmrec
    return fm2 + body2, rec


def apply(text, rec):
    fm, body = split_front_matter(text)
    fm2, o1 = apply_fm_test(fm, rec.get("front_matter_test"))
    body2, o2 = apply_body(body, rec)
    return fm2 + body2, o1 + o2


def is_empty(rec):
    return not rec["tags"] and not rec["inserts"] and not rec["front_matter_test"]


if __name__ == "__main__":
    cmd, src = sys.argv[1], Path(sys.argv[2])
    files = sorted(list(src.glob("assets/**/*.md")) + list(src.glob("content/**/*.md")))
    if cmd == "export":   # export <src> <overlay.json> <stripped_out_dir>
        overlay, dst = {}, Path(sys.argv[4])
        for f in files:
            rel = f.relative_to(src).as_posix()
            stripped, rec = export(f.read_text())
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            (dst / rel).write_text(stripped)
            if not is_empty(rec):
                overlay[rel] = rec
        Path(sys.argv[3]).write_text(json.dumps(overlay, indent=1))
        print(f"files with markup: {len(overlay)}")
    elif cmd == "apply":  # apply <stripped_dir> <overlay.json> <out_dir>
        overlay, dst = json.loads(Path(sys.argv[3]).read_text()), Path(sys.argv[4])
        orphans = {}
        for f in files:
            rel = f.relative_to(src).as_posix()
            text = f.read_text()
            if rel in overlay:
                text, o = apply(text, overlay[rel])
                if o:
                    orphans[rel] = o
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            (dst / rel).write_text(text)
        missing = [r for r in overlay if not (src / r).exists()]
        print(f"applied to {len(overlay) - len(missing)} files; files gone: {len(missing)}; files with orphans: {len(orphans)}; orphan entries: {sum(len(v) for v in orphans.values())}")
        Path(sys.argv[4]).joinpath("ORPHANS.json").write_text(json.dumps({"missing_files": missing, "orphans": orphans}, indent=1))
