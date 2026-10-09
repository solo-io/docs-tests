#!/usr/bin/env python3
"""Test annotations: doc-test markup kept in this repo and attached to pages at test time.

Content repos carry no test markup. Everything a doc test needs from a page --
the `paths="..."` tag on a code block's opener, the hidden
`{{< doc-test >}}...{{< /doc-test >}}` checks, and the front matter `test:` key
-- lives in an annotation file here, one per page, and is put back onto a
temporary copy of the page before the extractor runs.

An annotation finds its place by FINGERPRINT, a short hash of a code block's
text, never by line number, and never by storing the page's prose. Each piece
of markup ends a run in one of three states:

  exact         the block's fingerprint is unchanged
  close         the block in the same place was edited slightly, and it is the
                only nearly identical candidate (see `close_match`)
  needs-update  rewritten, deleted or ambiguous; the markup is kept in the
                annotation file, not attached, and reported

Commands (see `--help` on each):

  export   write annotation files from markup that is still inline in a repo
  strip    remove inline markup from a repo's pages, in place
  attach   put annotations back onto a repo's pages (in place, or into --out)
  refresh  re-attach after content changes, follow moved pages, and rewrite
           the annotation files with today's fingerprints
  annotate write one page with its markup attached, to edit by hand
  save     write a hand-edited page's markup back to its annotation file
  roundtrip  export, strip and re-attach a whole repo in memory and confirm
           every page comes back byte-identical
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

FORMAT_VERSION = 1

# Close-match rule, measured by replaying agentgateway/website history: at these
# values it placed 410 edited tested blocks right and 2 wrong. Looser values
# recover a few dozen more but add wrong placements, which nobody would notice.
MIN_SIMILARITY = 0.6
MIN_MARGIN = 0.1

PAGE_GLOBS = ("assets/**/*.md", "content/**/*.md")

FENCE = re.compile(r"^(\s*)(`{3,})(.*)$")
DT_OPEN = re.compile(r"\{\{[<%]\s*doc-test\b")
DT_CLOSE = re.compile(r"\{\{[<%]\s*/doc-test\s*[>%]\}\}")
PATHS_ATTR = re.compile(r'paths\s*=\s*"[^"]*"')
PATHS_VALUE = re.compile(r'paths\s*=\s*"([^"]*)"')
ATTR_TOKEN = re.compile(r'[\w-]+\s*=\s*"[^"]*"|[^\s,]+')
WORD = re.compile(r"[A-Za-z0-9_.-]+")

EXACT, CLOSE, NEEDS_UPDATE = "exact", "close", "needs-update"


def _hash(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def normalize_block(content_lines: List[str]) -> str:
    """A block's text with trailing spaces and common indentation removed.

    Indentation is ignored so that a block moved into or out of a list item
    keeps its fingerprint.
    """
    return textwrap.dedent("\n".join(l.rstrip() for l in content_lines)).strip()


def fingerprint(content_lines: List[str]) -> str:
    return _hash(normalize_block(content_lines))


def line_hashes(content_lines: List[str]) -> List[str]:
    return [_hash(x.strip(), 8) for x in normalize_block(content_lines).split("\n") if x.strip()]


def word_hashes(line: str) -> List[str]:
    """Hashes of a sentence's words; `succeeds.` and `succeeds` are one word."""
    return [_hash(w.strip(".-"), 8) for w in WORD.findall(line.lower()) if w.strip(".-")]


def similarity(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity of two hash sets."""
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else 1.0


def split_front_matter(text: str) -> Tuple[str, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[: end + 5], text[end + 5:]
        if text.rstrip("\n").endswith("\n---"):
            return text, ""
    return "", text


# --------------------------------------------------------------------- parsing
def scan(lines: List[str]):
    """Yield ('dt', i, j), ('fence', i, j, opener_match) or ('line', i) over a body."""
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


def language(info: str) -> str:
    info = info.strip()
    return re.split(r"[,{\s]", info, maxsplit=1)[0].lower() if info else ""


def strip_paths(info: str):
    """Remove paths="..." from an opener's info string. Returns (new_info, form).

    `form` records exactly how the attribute was written, separator included,
    so that `restore_paths` gives back the same bytes. Forms: `{paths="x"}`
    alone, inside `{a=b, paths="x"}`, or bare `lang,paths="x"`.
    """
    m = re.search(r"(\s*)\{([^}]*)\}", info)
    if m and PATHS_ATTR.search(m.group(2)):
        inner = m.group(2)
        toks = list(ATTR_TOKEN.finditer(inner))
        pos = next(k for k, t in enumerate(toks) if PATHS_ATTR.fullmatch(t.group(0)))
        attr = toks[pos].group(0)
        if len(toks) == 1:
            form = {"attr": attr, "only": True, "lead": m.group(1)}
            if inner != attr:
                form["inner"] = inner          # spaces inside the braces
            if info[m.end():]:
                form["tail"] = info[m.end():]
            return info[: m.start()] + info[m.end():], form
        if pos > 0:   # take the separator before the attribute with it
            a, b = toks[pos - 1].end(), toks[pos].end()
            sep = inner[a:toks[pos].start()]
        else:         # first attribute: take the separator after it
            a, b = toks[0].start(), toks[1].start()
            sep = inner[toks[0].end():b]
        new_inner = inner[:a] + inner[b:]
        return info[: m.start(2)] + new_inner + info[m.end(2):], {"attr": attr, "pos": pos, "sep": sep}
    b = re.search(r',\s*paths\s*=\s*"[^"]*"', info)
    if b:
        return info[: b.start()] + info[b.end():], {"bare": b.group(0), "at": b.start()}
    return info, None


def restore_paths(info: str, form: dict) -> str:
    if "bare" in form:
        at = min(form["at"], len(info))
        return info[:at] + form["bare"] + info[at:]
    if form.get("only"):
        braces = "{" + form.get("inner", form["attr"]) + "}"
        tail = form.get("tail", "")
        if tail and info.endswith(tail):
            return info[: len(info) - len(tail)] + form["lead"] + braces + tail
        return info + (form.get("lead") or " ") + braces
    m = re.search(r"\{([^}]*)\}", info)
    if not m:
        # The opener lost its attribute list since export (a close match); add one.
        return info + " {" + form["attr"] + "}"
    inner = m.group(1)
    toks = list(ATTR_TOKEN.finditer(inner))
    pos = min(form["pos"], len(toks))
    if pos > 0:
        at = toks[pos - 1].end()
        inner = inner[:at] + form["sep"] + form["attr"] + inner[at:]
    else:
        at = toks[0].start() if toks else 0
        inner = inner[:at] + form["attr"] + (form["sep"] if toks else "") + inner[at:]
    return info[: m.start(1)] + inner + info[m.end(1):]


@dataclass
class Block:
    fp: str
    lang: str
    lines: List[str]
    opener: int       # line index of the opener, in the page's line list
    close: int        # line index of the closing fence
    preview: str


@dataclass
class Prose:
    idx: int
    h: str
    occ: int
    prev_block: int   # index of the last code block before this line, -1 if none
    words: List[str]


@dataclass
class Page:
    """A page with markup removed, and the markup that was on it."""
    lines: List[str]          # the stripped page: front matter + body
    body_start: int           # index of the first body line in `lines`
    blocks: List[Block]
    prose: List[Prose]
    tags: List[dict] = field(default_factory=list)
    hidden: List[dict] = field(default_factory=list)
    front_matter: Optional[dict] = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def has_markup(self) -> bool:
        return bool(self.tags or self.hidden or self.front_matter)


def _strip_front_matter_test(fm: str):
    if not fm:
        return fm, None
    lines = fm.split("\n")
    for i, l in enumerate(lines):
        if i and re.match(r"^test:", l):
            j = i + 1
            while j < len(lines) and (lines[j] == "" or lines[j][:1] in (" ", "\t", "-")) and lines[j] != "---":
                j += 1
            prev = lines[i - 1]
            rec = {"after_line": _hash(prev), "occurrence": lines[: i - 1].count(prev),
                   "content": "\n".join(lines[i:j])}
            return "\n".join(lines[:i] + lines[j:]), rec
    return fm, None


def parse(text: str, preview: bool = True) -> Page:
    """Split a page into its stripped text and the markup on it.

    Works the same on a page with no markup, which is how a content repo's
    pages look once markup lives only here.
    """
    fm, body = split_front_matter(text)
    fm, fm_rec = _strip_front_matter_test(fm)
    if fm.endswith("\n"):
        fm_lines, src = fm.split("\n")[:-1], body.split("\n")
    elif fm:
        fm_lines, src = fm.split("\n"), []    # the page ends at its front matter
    else:
        fm_lines, src = [], body.split("\n")
    out: List[str] = list(fm_lines)
    kinds: List[tuple] = [("fm",)] * len(fm_lines)
    blocks: List[Block] = []
    prose: List[Prose] = []
    tags: List[dict] = []
    hidden: List[dict] = []
    line_occ: Dict[str, int] = {}

    def anchor():
        for k in range(len(kinds) - 1, -1, -1):
            kind = kinds[k]
            if kind[0] == "close":
                b = blocks[kind[1]]
                return {"block": kind[1], "fingerprint": b.fp, "lang": b.lang, "lines": b.lines}, k
            if kind[0] == "line":
                p = kind[1]
                return {"line": p.h, "occurrence": p.occ, "block_before": p.prev_block, "words": p.words}, k
            if kind[0] == "fm":
                break
        return {"start": True}, len(fm_lines) - 1

    for tok in scan(src):
        if tok[0] == "dt":
            _, i, j = tok
            blank = []
            while out and kinds[-1][0] == "blank":
                blank.insert(0, out.pop())
                kinds.pop()
            a, at = anchor()
            hidden.append({"after": a, "at": at, "blank_before": blank, "content": "\n".join(src[i:j + 1])})
        elif tok[0] == "fence":
            _, i, j, m = tok
            content = src[i + 1:j]
            new_info, form = strip_paths(m.group(3))
            bi = len(blocks)
            first = normalize_block(content).split("\n")[0][:80]
            blocks.append(Block(fingerprint(content), language(new_info), line_hashes(content),
                                len(out), len(out) + len(content) + 1, first if preview else ""))
            if form:
                tags.append({"block": bi, "paths": PATHS_VALUE.search(form.get("attr") or form["bare"]).group(1),
                             "form": form})
            out.append(m.group(1) + m.group(2) + new_info)
            kinds.append(("in",))
            for c in content:
                out.append(c)
                kinds.append(("in",))
            if j < len(src):
                out.append(src[j])
                kinds.append(("close", bi))
            else:
                blocks[-1].close = len(out) - 1
        else:
            l = src[tok[1]]
            if not l.strip():
                out.append(l)
                kinds.append(("blank",))
            else:
                lh = _hash(l.strip())
                occ = line_occ.get(lh, 0)
                line_occ[lh] = occ + 1
                p = Prose(len(out), lh, occ, len(blocks) - 1, word_hashes(l))
                prose.append(p)
                out.append(l)
                kinds.append(("line", p))
    return Page(out, len(fm_lines), blocks, prose, tags, hidden, fm_rec)


# ----------------------------------------------------------------- annotation
def _join(hs: List[str]) -> str:
    return " ".join(hs)


def _split(s) -> List[str]:
    return s.split() if isinstance(s, str) else list(s or [])


def to_annotation(source: str, page: Page, preview: bool = True) -> dict:
    """The annotation file for a parsed page (markup only, no prose)."""
    def block_ref(bi):
        b = page.blocks[bi]
        ref = {"block": bi, "fingerprint": b.fp, "lang": b.lang, "lines": _join(b.lines)}
        if preview and b.preview:
            ref["preview"] = b.preview
        return ref

    tags = []
    for t in page.tags:
        entry = {"paths": t["paths"], **block_ref(t["block"]), "form": t["form"]}
        tags.append(entry)
    hidden = []
    for h in page.hidden:
        a = h["after"]
        if "start" in a:
            after = {"start": True}
        elif "block" in a:
            after = block_ref(a["block"])
        else:
            after = {"line": a["line"], "occurrence": a["occurrence"], "words": _join(a["words"]),
                     "block_before": block_ref(a["block_before"]) if a["block_before"] >= 0 else None}
        entry = {"after": after}
        if h["blank_before"] != [""]:
            entry["blank_before"] = h["blank_before"]
        entry["content"] = h["content"]
        hidden.append(entry)
    ann = {"format": FORMAT_VERSION, "source": source}
    if not preview:
        # Sticky: refresh and save keep previews off for this page, so a page
        # from a private repo never has its text copied in by a later run.
        ann["previews"] = False
    ann["blocks"] = [f"{b.fp} {b.lang}".rstrip() for b in page.blocks]
    if tags:
        ann["tags"] = tags
    if hidden:
        ann["hidden"] = hidden
    if page.front_matter:
        ann["front_matter"] = page.front_matter
    return ann


def count_markup(ann: dict) -> int:
    return len(ann.get("tags", [])) + len(ann.get("hidden", [])) + (1 if ann.get("front_matter") else 0)


class _Dumper(yaml.SafeDumper):
    pass


def _str_repr(dumper, s):
    if "\n" in s:
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", s)


_Dumper.add_representer(str, _str_repr)


def dump_annotation(ann: dict) -> str:
    return yaml.dump(ann, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=10_000)


def load_annotation(path: Path) -> dict:
    ann = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(ann, dict) or ann.get("format") != FORMAT_VERSION:
        raise ValueError(f"{path}: not a format {FORMAT_VERSION} annotation file")
    return ann


def annotation_path(annotations_dir: Path, source: str) -> Path:
    return annotations_dir / (source + ".yaml")


# ----------------------------------------------------------------- matching
@dataclass
class Placement:
    kind: str                 # tag | hidden | front_matter
    label: str                # paths value, or the hidden block's first line
    state: str                # exact | close | needs-update
    reason: str = ""
    score: Optional[float] = None
    at: Optional[int] = None  # tag: block index; hidden: the stripped-page line it follows


def close_match(old_lines: List[str], old_lang: str, new_blocks: List[Block], candidates: List[int]):
    """Pick the one candidate block nearly identical to an edited block.

    Candidates are the new blocks between the same unchanged neighbors (see
    `map_blocks`). The best must have the same language, a line similarity of
    at least MIN_SIMILARITY, and lead the runner-up by at least MIN_MARGIN.
    Returns (new index or None, best score, runner-up score).
    """
    scored = sorted(((similarity(old_lines, new_blocks[j].lines), j)
                     for j in candidates if new_blocks[j].lang == old_lang), reverse=True)
    if not scored:
        return None, 0.0, 0.0
    best, j = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best >= MIN_SIMILARITY and best - second >= MIN_MARGIN:
        return j, best, second
    return None, best, second


def align(old_fps: List[str], new_fps: List[str]) -> Dict[int, tuple]:
    """Old block index -> ('exact', j) | ('region', [js]) | ('gone', []).

    Blocks in an `equal` run keep their place. Old blocks in a `replace` run
    sit between unchanged neighbors, and the new blocks of that run are their
    close-match candidates. An old block whose fingerprint occurs exactly once
    among the new blocks that did not align is exact too: it moved unchanged.
    """
    res: Dict[int, tuple] = {}
    taken = set()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_fps, new_fps, autojunk=False).get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                res[i1 + k] = ("exact", j1 + k)
                taken.add(j1 + k)
        elif op == "replace":
            for i in range(i1, i2):
                res[i] = ("region", list(range(j1, j2)))
        elif op == "delete":
            for i in range(i1, i2):
                res[i] = ("gone", [])
    loose: Dict[str, List[int]] = {}
    for j, fp in enumerate(new_fps):
        if j not in taken:
            loose.setdefault(fp, []).append(j)
    for i, r in list(res.items()):
        if r[0] != "exact" and len(loose.get(old_fps[i], [])) == 1:
            j = loose[old_fps[i]][0]
            res[i] = ("exact", j)
            taken.add(j)
    for i, r in list(res.items()):
        if r[0] == "region":
            res[i] = ("region", [j for j in r[1] if j not in taken])
    return res


def map_blocks(ann: dict, page: Page, refs: List[dict]) -> Dict[int, tuple]:
    """Resolve every old block the markup refers to: old index -> (state, new index, score)."""
    old_fps = [b.split()[0] for b in ann.get("blocks", [])]
    aligned = align(old_fps, [b.fp for b in page.blocks])
    wanted: Dict[int, dict] = {}
    for r in refs:
        if r.get("block") is not None:
            wanted.setdefault(r["block"], r)
    out: Dict[int, tuple] = {}
    for bi, ref in wanted.items():
        state, cands = aligned.get(bi, ("gone", []))
        if bi >= len(old_fps) or old_fps[bi] != ref["fingerprint"]:
            state, cands = "gone", []     # annotation edited by hand; fall back to search below
        if state == "exact":
            out[bi] = (EXACT, cands, None)
            continue
        if state == "region":
            j, score, _ = close_match(_split(ref["lines"]), ref.get("lang", ""), page.blocks, cands)
            if j is not None:
                out[bi] = (CLOSE, j, score)
                continue
            out[bi] = (NEEDS_UPDATE, None, score)
            continue
        same = [j for j, b in enumerate(page.blocks) if b.fp == ref["fingerprint"]]
        out[bi] = (EXACT, same[0], None) if len(same) == 1 else (NEEDS_UPDATE, None, None)
    # Two edited blocks must not land on the same new block.
    landed: Dict[int, List[int]] = {}
    for bi, (state, j, _) in out.items():
        if state == CLOSE:
            landed.setdefault(j, []).append(bi)
    for j, bis in landed.items():
        if len(bis) > 1:
            for bi in bis:
                out[bi] = (NEEDS_UPDATE, None, out[bi][2])
    return out


def _orphan_ref(ref: dict) -> dict:
    """An entry whose block was not found: drop its index so it is searched by fingerprint."""
    return {k: v for k, v in ref.items() if k != "block"}


def attach(text: str, ann: dict) -> Tuple[str, List[Placement], dict]:
    """Put an annotation's markup onto a page.

    `text` may still carry inline markup (a repo mid-migration); it is removed
    first, so the result depends only on the annotation. Returns the marked-up
    page, one Placement per piece of markup, and the entries that did not attach
    (to keep in the annotation file, never to drop).
    """
    page = parse(text, preview=False)
    lines = list(page.lines)
    tags = ann.get("tags", [])
    hidden = ann.get("hidden", [])
    refs = list(tags)
    for h in hidden:
        a = h["after"]
        if "fingerprint" in a:
            refs.append(a)
        elif a.get("block_before"):
            refs.append(a["block_before"])
    mapped = map_blocks(ann, page, [r for r in refs if r.get("block") is not None])
    for r in refs:
        if r.get("block") is None:
            same = [j for j, b in enumerate(page.blocks) if b.fp == r["fingerprint"]]
            mapped[id(r)] = (EXACT, same[0], None) if len(same) == 1 else (NEEDS_UPDATE, None, None)

    def resolve(ref):
        key = ref["block"] if ref.get("block") is not None else id(ref)
        return mapped.get(key, (NEEDS_UPDATE, None, None))

    placements: List[Placement] = []
    unplaced = {"tags": [], "hidden": [], "front_matter": None}

    # tags
    on_opener: Dict[int, dict] = {}
    for t in tags:
        state, j, score = resolve(t)
        if state != NEEDS_UPDATE and j in on_opener:
            state, j, reason = NEEDS_UPDATE, None, "another tag already landed on this block"
        else:
            reason = "" if state != NEEDS_UPDATE else "tagged block was rewritten or removed"
        if state == NEEDS_UPDATE:
            placements.append(Placement("tag", t["paths"], state, reason, score))
            unplaced["tags"].append(_orphan_ref(t))
            continue
        on_opener[j] = t
        placements.append(Placement("tag", t["paths"], state, "", score, j))
    for j, t in on_opener.items():
        b = page.blocks[j]
        m = FENCE.match(lines[b.opener])
        lines[b.opener] = m.group(1) + m.group(2) + restore_paths(m.group(3), t["form"])

    # hidden checks: after the page start, after a block, or after a sentence
    after: Dict[int, List[dict]] = {}
    for h in hidden:
        a = h["after"]
        first = h["content"].split("\n")[0]
        label = PATHS_VALUE.search(first).group(1) if PATHS_VALUE.search(first) else first.strip()
        if a.get("start"):
            after.setdefault(page.body_start - 1, []).append(h)
            placements.append(Placement("hidden", label, EXACT, at=page.body_start - 1))
            continue
        if "fingerprint" in a:
            state, j, score = resolve(a)
            if state == NEEDS_UPDATE:
                placements.append(Placement("hidden", label, state, "block it follows was rewritten or removed", score))
                unplaced["hidden"].append({**h, "after": _orphan_ref(a)})
                continue
            after.setdefault(page.blocks[j].close, []).append(h)
            placements.append(Placement("hidden", label, state, "", score, page.blocks[j].close))
            continue
        # A sentence anchor is looked for between the block before it and the
        # next one. That block may have been close-matched: the check still
        # moves only with a block, never across the page on its own.
        if a.get("block_before"):
            wstate, window, _ = resolve(a["block_before"])
        else:
            window, wstate = -1, EXACT
        if wstate == NEEDS_UPDATE or window is None:
            placements.append(Placement("hidden", label, NEEDS_UPDATE, "the code block before its sentence was rewritten or removed"))
            unplaced["hidden"].append({**h, "after": {**a, "block_before": _orphan_ref(a["block_before"])}})
            continue
        cands = [p for p in page.prose if p.prev_block == window]
        exact = [p for p in cands if p.h == a["line"]]
        if exact:
            pick = next((p for p in exact if p.occ == a.get("occurrence")), exact[0])
            after.setdefault(pick.idx, []).append(h)
            placements.append(Placement("hidden", label, wstate, at=pick.idx))
            continue
        scored = sorted(((similarity(_split(a["words"]), p.words), k) for k, p in enumerate(cands)), reverse=True)
        best = scored[0][0] if scored else 0.0
        second = scored[1][0] if len(scored) > 1 else 0.0
        if scored and best >= MIN_SIMILARITY and best - second >= MIN_MARGIN:
            after.setdefault(cands[scored[0][1]].idx, []).append(h)
            placements.append(Placement("hidden", label, CLOSE, "", best, cands[scored[0][1]].idx))
            continue
        placements.append(Placement("hidden", label, NEEDS_UPDATE, "the sentence it follows was rewritten", best))
        unplaced["hidden"].append({**h, "after": {**a, "block_before": _orphan_ref(a["block_before"]) if a.get("block_before") else None}})

    out: List[str] = lines[:page.body_start]
    for h in after.get(page.body_start - 1, []):
        out += h.get("blank_before", [""]) + h["content"].split("\n")
    for i in range(page.body_start, len(lines)):
        out.append(lines[i])
        for h in after.get(i, []):
            out += h.get("blank_before", [""]) + h["content"].split("\n")

    # front matter test:, inserted after the line it followed
    rec = ann.get("front_matter")
    if rec:
        seen, done = 0, False
        for i in range(page.body_start):
            if _hash(out[i]) == rec["after_line"]:
                if seen == rec["occurrence"]:
                    out[i + 1:i + 1] = rec["content"].split("\n")
                    done = True
                    break
                seen += 1
        if done:
            placements.append(Placement("front_matter", "test:", EXACT))
        elif page.body_start >= 2 and out[page.body_start - 1] == "---":
            # `test:` is a top-level key, so where it sits does not change its
            # meaning; the end of the front matter is as good as anywhere.
            out[page.body_start - 1:page.body_start - 1] = rec["content"].split("\n")
            placements.append(Placement("front_matter", "test:", CLOSE, "moved to the end of the front matter"))
        else:
            placements.append(Placement("front_matter", "test:", NEEDS_UPDATE, "the page has no front matter"))
            unplaced["front_matter"] = rec
    return "\n".join(out), placements, unplaced


# ------------------------------------------------------------------ repo I/O
def page_files(repo: Path, prefixes: Iterable[str] = ()) -> List[str]:
    rels = sorted({p.relative_to(repo).as_posix() for g in PAGE_GLOBS for p in repo.glob(g)})
    prefixes = list(prefixes)
    return [r for r in rels if not prefixes or any(r.startswith(p) or f"/{p}" in r for p in prefixes)]


def annotation_files(annotations_dir: Path) -> List[Path]:
    return sorted(p for p in annotations_dir.rglob("*.md.yaml"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def renames_since(repo: Path, since: str) -> Dict[str, Optional[str]]:
    """Pages renamed or deleted since a commit: old path -> new path (None if deleted)."""
    moved: Dict[str, Optional[str]] = {}
    for line in git(repo, "diff", "--name-status", "-M", since, "HEAD", "--", *PAGE_GLOBS).splitlines():
        f = line.split("\t")
        if f[0].startswith("R"):
            moved[f[1]] = f[2]
        elif f[0] == "D":
            moved[f[1]] = None
    return moved


def changed_since(repo: Path, since: str) -> set:
    return set(git(repo, "diff", "--name-only", "-M", since, "HEAD", "--", *PAGE_GLOBS).split())


def find_moved_page(repo: Path, ann: dict, taken: set, index: Dict[str, set]) -> Optional[str]:
    """Find a page that went missing by its tagged blocks' fingerprints.

    A page qualifies when it holds every fingerprint the annotation tags. Pages
    that already have an annotation are passed over, because versioned trees
    hold copies of the same page; if that still leaves more than one, the page
    is not moved, and its markup is reported instead.
    """
    fps = {t["fingerprint"] for t in ann.get("tags", [])}
    fps |= {h["after"]["fingerprint"] for h in ann.get("hidden", []) if "fingerprint" in h["after"]}
    if not fps:
        return None
    cands = set.intersection(*(index.get(fp, set()) for fp in fps)) - taken
    return cands.pop() if len(cands) == 1 else None


def fingerprint_index(repo: Path, files: List[str]) -> Dict[str, set]:
    index: Dict[str, set] = {}
    for rel in files:
        for b in parse(read(repo / rel), preview=False).blocks:
            index.setdefault(b.fp, set()).add(rel)
    return index


# ------------------------------------------------------------------ commands
def _summary(placements: List[Placement]) -> Dict[str, int]:
    c = {EXACT: 0, CLOSE: 0, NEEDS_UPDATE: 0}
    for p in placements:
        c[p.state] += 1
    return c


def cmd_export(args) -> int:
    repo, out = Path(args.repo), Path(args.annotations)
    n = 0
    for rel in page_files(repo, args.prefix):
        page = parse(read(repo / rel), preview=not args.no_preview)
        if page.front_matter and args.front_matter != "all":
            value = (yaml.safe_load(page.front_matter["content"]) or {}).get("test")
            if args.front_matter == "none" or value != "skip":
                page.front_matter = None
        if not page.has_markup():
            continue
        ann = to_annotation(rel, page, preview=not args.no_preview)
        dest = annotation_path(out, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(dump_annotation(ann), encoding="utf-8")
        n += 1
    print(f"wrote {n} annotation files to {out}")
    return 0


def cmd_strip(args) -> int:
    repo = Path(args.repo)
    n = 0
    for rel in page_files(repo, args.prefix):
        text = read(repo / rel)
        page = parse(text, preview=False)
        if page.has_markup():
            (repo / rel).write_text(page.text, encoding="utf-8")
            n += 1
    print(f"removed markup from {n} pages")
    return 0


def attach_repo(repo: Path, annotations_dir: Path, out: Optional[Path], only: Optional[set] = None):
    """Attach every annotation file. Returns {source: [Placement]} and missing sources."""
    results: Dict[str, List[Placement]] = {}
    missing: List[str] = []
    for path in annotation_files(annotations_dir):
        ann = load_annotation(path)
        src = ann["source"]
        if only is not None and src not in only:
            continue
        if not (repo / src).exists():
            missing.append(src)
            continue
        text, placements, _ = attach(read(repo / src), ann)
        dest = (out or repo) / src
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        results[src] = placements
    return results, missing


def write_report(results: Dict[str, List[Placement]], missing: List[str], path: Optional[str],
                 repo: Optional[Path] = None) -> Dict[str, int]:
    """Totals, plus a JSON report of every piece of markup that did not attach exactly.

    `doc_test_run.py --annotation-report` reads it to skip scenarios whose
    markup is needs update; `repo` says which checkout its page paths are in.
    """
    total = {EXACT: 0, CLOSE: 0, NEEDS_UPDATE: 0}
    rows = []
    for src, ps in sorted(results.items()):
        for p in ps:
            total[p.state] += 1
            if p.state != EXACT:
                rows.append({"page": src, **p.__dict__})
    if path:
        Path(path).write_text(json.dumps({"repo": str(repo.resolve()) if repo else None, "totals": total,
                                          "pages_missing": missing, "changed": rows}, indent=1))
    return total


def write_summary(results: Dict[str, List[Placement]], missing: List[str], path: str) -> None:
    """Append the tested blocks that changed to a GitHub job summary."""
    rows = [(src, p) for src, ps in sorted(results.items()) for p in ps if p.state != EXACT]
    if not rows and not missing:
        return
    lines = ["## Doc tests: changed tested blocks", "",
             "Close matches ran. Needs update means the block changed too much for its test to follow "
             "it; scenarios that use it are skipped with a warning, and the daily refresh updates the "
             "test after merge.", "",
             "| Page | Markup | State | Note |", "| --- | --- | --- | --- |"]
    lines += [f"| {src} | {p.kind} `{p.label}` | {p.state} | {p.reason} |" for src, p in rows]
    lines += [f"| {src} | page | needs-update | page not found |" for src in missing]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


def cmd_attach(args) -> int:
    repo = Path(args.repo)
    only = set(args.page) if args.page else None
    results, missing = attach_repo(repo, Path(args.annotations), Path(args.out) if args.out else None, only)
    total = write_report(results, missing, args.report, repo)
    if args.summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        write_summary(results, missing, os.environ["GITHUB_STEP_SUMMARY"])
    print(f"attached {sum(len(v) for v in results.values())} pieces of markup on {len(results)} pages: "
          f"{total[EXACT]} exact, {total[CLOSE]} close, {total[NEEDS_UPDATE]} needs update; "
          f"{len(missing)} pages missing")
    for src, ps in sorted(results.items()):
        for p in ps:
            if p.state == NEEDS_UPDATE:
                print(f"needs update: {src}: {p.kind} {p.label}: {p.reason}")
    for src in missing:
        print(f"needs update: {src}: page not found (run refresh to follow a moved page)")
    return 0


def refresh_one(text: str, ann: dict, source: str, preview: bool) -> Tuple[dict, List[Placement]]:
    """Re-attach an annotation to today's page and record today's fingerprints."""
    preview = preview and ann.get("previews", True)
    attached, placements, unplaced = attach(text, ann)
    new = to_annotation(source, parse(attached, preview=preview), preview=preview)
    if unplaced["tags"]:
        new.setdefault("tags", []).extend({**t, "status": NEEDS_UPDATE} for t in unplaced["tags"])
    if unplaced["hidden"]:
        new.setdefault("hidden", []).extend({**h, "status": NEEDS_UPDATE} for h in unplaced["hidden"])
    if unplaced["front_matter"]:
        new["front_matter"] = {**unplaced["front_matter"], "status": NEEDS_UPDATE}
    if count_markup(new) != count_markup(ann):
        raise RuntimeError(f"{source}: refresh would change the markup count from "
                           f"{count_markup(ann)} to {count_markup(new)}; refusing")
    return new, placements


def cmd_refresh(args) -> int:
    repo, adir = Path(args.repo), Path(args.annotations)
    state_file = adir / "refresh-state.yaml"
    since = args.since
    if not since and state_file.exists():
        since = (yaml.safe_load(read(state_file)) or {}).get("commit")
    head = git(repo, "rev-parse", "HEAD").strip()
    moved = renames_since(repo, since) if since else {}
    changed = changed_since(repo, since) if since else None
    preview = not args.no_preview

    loaded = [(p, load_annotation(p)) for p in annotation_files(adir)]
    before = sum(count_markup(a) for _, a in loaded)
    taken = {a["source"] for _, a in loaded}
    index = None
    writes: Dict[Path, Tuple[Optional[Path], dict]] = {}
    results: Dict[str, List[Placement]] = {}
    moves: List[Tuple[str, str]] = []
    for path, ann in loaded:
        src = ann["source"]
        new_src = src
        if not (repo / src).exists():
            new_src = moved.get(src)
            if not new_src or not (repo / new_src).exists():
                if index is None:
                    index = fingerprint_index(repo, page_files(repo))
                new_src = find_moved_page(repo, ann, taken, index)
            if not new_src:
                results[src] = [Placement("page", src, NEEDS_UPDATE, "page not found")]
                continue
            taken.add(new_src)
            moves.append((src, new_src))
        elif changed is not None and src not in changed and not any(
                e.get("status") for e in ann.get("tags", []) + ann.get("hidden", [])):
            continue
        new, placements = refresh_one(read(repo / new_src), ann, new_src, preview)
        results[new_src] = placements
        writes[path] = (annotation_path(adir, new_src) if new_src != src else None, new)

    after = before - sum(count_markup(a) for p, a in loaded if p in writes) + sum(count_markup(n) for _, n in writes.values())
    if after != before:
        print(f"refusing to write: markup count would go from {before} to {after}", file=sys.stderr)
        return 1
    if not args.dry_run:
        for path, (dest, new) in writes.items():
            target = dest or path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dump_annotation(new), encoding="utf-8")
            if dest and dest != path:
                path.unlink()
        state_file.write_text(dump_annotation({"commit": head}), encoding="utf-8")
    total = write_report(results, [], args.report, repo)
    stats = set_stats(adir, results)
    if args.report:
        report = json.loads(Path(args.report).read_text())
        report.update({"commit": head, "pages_refreshed": len(writes), **stats,
                       "moved": [{"from": a, "to": b} for a, b in moves],
                       "close": [r for r in report["changed"] if r["state"] == CLOSE]})
        Path(args.report).write_text(json.dumps(report, indent=1))
    print(f"refresh to {head[:12]}: {len(writes)} pages re-attached, {len(moves)} moved; "
          f"{total[EXACT]} exact, {total[CLOSE]} close, {total[NEEDS_UPDATE]} needs update")
    print(f"needs update in total: {len(stats['needs_update'])}")
    for a, b in moves:
        print(f"moved: {a} -> {b}")
    for src, ps in sorted(results.items()):
        for p in ps:
            if p.state != EXACT:
                print(f"{p.state}: {src}: {p.kind} {p.label}" + (f" ({p.reason})" if p.reason else ""))
    return 0


def section_of(page: str) -> str:
    """The docs section a page belongs to, such as `traffic-management` or `mcp`.

    The first directory below the version root (content/<...>/<mode>/<version>/)
    or below assets' `pages/`, skipping wrappers that hold every section:
    `documentation`, `configuration`, and a product directory under `pages/`.
    A snippet outside `pages/` is reported as `snippets`.
    """
    segs = page.split("/")[:-1]
    if segs and segs[0] == "assets":
        if "pages" not in segs:
            return "snippets"
        rest = segs[segs.index("pages") + 1:]
        if rest and rest[0] == "agentgateway":
            rest = rest[1:]
    else:
        vi = next((i for i, x in enumerate(segs) if x in ("latest", "main") or re.fullmatch(r"\d+\.\d+\.x", x)), None)
        rest = segs[vi + 1:] if vi is not None else segs[1:]
    rest = [x for x in rest if x not in ("documentation", "configuration")]
    return rest[0] if rest else "(top level)"


def set_stats(adir: Path, results: Dict[str, List[Placement]]) -> dict:
    """Totals over the whole annotation set, for the daily report."""
    pages, markup, needs, sections = 0, 0, [], {}
    for path in annotation_files(adir):
        ann = load_annotation(path)
        n = count_markup(ann)
        pages += 1
        markup += n
        sec = sections.setdefault(section_of(ann["source"]), {"pages": 0, "markup": 0})
        sec["pages"] += 1
        sec["markup"] += n
        for kind in ("tags", "hidden"):
            for e in ann.get(kind, []):
                if e.get("status") == NEEDS_UPDATE:
                    label = e["paths"] if kind == "tags" else (
                        PATHS_VALUE.search(e["content"].split("\n")[0]) or [None, "hidden check"])[1]
                    needs.append({"page": ann["source"], "kind": kind.rstrip("s"), "label": label})
        if (ann.get("front_matter") or {}).get("status") == NEEDS_UPDATE:
            needs.append({"page": ann["source"], "kind": "front_matter", "label": "test:"})
    for src, ps in results.items():
        for p in ps:
            if p.kind == "page":
                needs.append({"page": src, "kind": "page", "label": "page not found"})
    return {"pages": pages, "markup_total": markup, "sections": dict(sorted(sections.items())),
            "needs_update": needs}


def slack_payloads(report: Optional[dict], run_url: Optional[str]) -> Tuple[dict, Optional[dict]]:
    """Slack Block Kit payloads for a refresh, laid out like the doc test results post.

    Main message: counts, the set by section, and every piece of markup that
    needs update. Thread: the day's close matches and moved pages, which were
    committed without review, so a person can check them.
    """
    from report_summary import _SLACK_MAX_BLOCKS, _run_url_block, _truncate  # noqa: E402

    if not report:
        header = "\u274c Annotation Refresh \u2014 failed; tests keep running on the last annotations"
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": header[:150]}}]
        if run_url:
            blocks.append(_run_url_block(run_url))
        return {"text": header, "blocks": blocks}, None

    needs = report["needs_update"]
    total = report["markup_total"]
    if needs:
        header = (f"\u26a0\ufe0f Annotation Refresh \u2014 {total - len(needs)} attached | "
                  f"{len(needs)} need update | {total} total")
    else:
        header = f"\u2705 Annotation Refresh \u2014 {total} attached | {total} total"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": header[:150]}}]
    lines = [f"*Annotations \u2014 {total} pieces of markup on {report['pages']} pages*"]
    for name, sec in report["sections"].items():
        lines.append(f"  `{name}`: {sec['markup']} on {sec['pages']} pages")
    lines.append(f"  Today: {report['pages_refreshed']} pages re-attached, {len(report['close'])} close "
                 f"matches, {len(report['moved'])} moved")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _truncate("\n".join(lines))}})
    if needs:
        body = "\n".join(f"\u26a0\ufe0f  `{n['label']}` \u2014 {n['kind']}  (_`{n['page']}`_)" for n in needs)
    else:
        body = "\u2705 Nothing needs update."
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _truncate(body)}})
    if run_url:
        blocks.append(_run_url_block(run_url))
    main_payload = {"text": header, "blocks": blocks}

    review = [f"\U0001f501  `{r['label']}` \u2014 {r['kind']} close match"
              + (f", similarity {r['score']:.2f}" if r.get("score") else "") + f"  (_`{r['page']}`_)"
              for r in report["close"]]
    review += [f"\U0001f4c4  moved: `{m['from']}` \u2192 `{m['to']}`" for m in report["moved"]]
    if not review:
        return main_payload, None
    thread = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Committed without review today ({len(review)})*"}}]
    for i in range(0, len(review), 20):
        if len(thread) >= _SLACK_MAX_BLOCKS - 1:
            break
        thread.append({"type": "section", "text": {"type": "mrkdwn", "text": _truncate("\n".join(review[i:i + 20]))}})
    return main_payload, {"text": f"Committed without review today ({len(review)})", "blocks": thread}


def cmd_slack(args) -> int:
    report = json.loads(read(Path(args.report))) if args.report and Path(args.report).is_file() and not args.failed else None
    main_payload, thread = slack_payloads(report, args.run_url)
    print(json.dumps({"main": main_payload, "thread": thread}))
    return 0


def cmd_annotate(args) -> int:
    repo, adir = Path(args.repo), Path(args.annotations)
    path = annotation_path(adir, args.page)
    text = read(repo / args.page)
    if path.exists():
        text, placements, _ = attach(text, load_annotation(path))
        for p in placements:
            if p.state != EXACT:
                print(f"{p.state}: {p.kind} {p.label}" + (f" ({p.reason})" if p.reason else ""))
    else:
        text = parse(text, preview=False).text
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}; add or edit markup there, then run save")
    return 0


def save(page_text: str, edited_text: str, source: str, old: Optional[dict], preview: bool = True) -> dict:
    """The annotation for a hand-edited copy of a page.

    Refuses if the copy differs from the page anywhere but in its markup: prose
    and code edits belong in a content PR, and saving them here would leave the
    annotation describing a page that does not exist.
    """
    preview = preview and (old or {}).get("previews", True)
    edited = parse(edited_text, preview=preview)
    current = parse(page_text, preview=False)
    if edited.text != current.text:
        diff = "\n".join(list(difflib.unified_diff(current.lines, edited.lines, "page", "edited copy", lineterm="", n=1))[:20])
        raise ValueError(f"{source}: the edited copy changes the page itself, not only its markup:\n{diff}")
    ann = to_annotation(source, edited, preview=preview)
    if old:
        kept_paths = {t["paths"] for t in ann.get("tags", [])}
        kept_hidden = {h["content"] for h in ann.get("hidden", [])}
        stale_tags = [t for t in old.get("tags", []) if t.get("status") == NEEDS_UPDATE and t["paths"] not in kept_paths]
        stale_hidden = [h for h in old.get("hidden", []) if h.get("status") == NEEDS_UPDATE and h["content"] not in kept_hidden]
        if stale_tags:
            ann.setdefault("tags", []).extend(stale_tags)
        if stale_hidden:
            ann.setdefault("hidden", []).extend(stale_hidden)
    return ann


def cmd_save(args) -> int:
    repo, adir = Path(args.repo), Path(args.annotations)
    path = annotation_path(adir, args.page)
    old = load_annotation(path) if path.exists() else None
    try:
        ann = save(read(repo / args.page), read(Path(args.edited)), args.page, old, preview=not args.no_preview)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    stale = [e for e in ann.get("tags", []) + ann.get("hidden", []) if e.get("status") == NEEDS_UPDATE]
    if count_markup(ann) == 0:
        if path.exists():
            path.unlink()
            print(f"removed {path}: the page has no markup left")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_annotation(ann), encoding="utf-8")
    print(f"wrote {path}: {count_markup(ann)} pieces of markup"
          + (f", {len(stale)} still marked needs update (delete them from the file if the test is gone)" if stale else ""))
    return 0


def cmd_roundtrip(args) -> int:
    """Export, strip and re-attach every page in memory; every page must come back identical."""
    repo = Path(args.repo)
    bad, n = [], 0
    for rel in page_files(repo, args.prefix):
        text = read(repo / rel)
        page = parse(text)
        if not page.has_markup():
            continue
        n += 1
        ann = yaml.safe_load(dump_annotation(to_annotation(rel, page)))
        back, placements, _ = attach(page.text, ann)
        if back != text or any(p.state != EXACT for p in placements):
            bad.append(rel)
    print(f"round trip: {n - len(bad)} of {n} pages with markup came back byte-identical")
    for rel in bad[:50]:
        print(f"differs: {rel}")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, annotations=True):
        p.add_argument("--repo", required=True, help="content repo checkout")
        if annotations:
            p.add_argument("--annotations", required=True, help="products/<product>/annotations")
        p.add_argument("--no-preview", action="store_true", help="leave each block's first line out of annotation files")

    p = sub.add_parser("export", help=cmd_export.__doc__)
    common(p)
    p.add_argument("--prefix", action="append", default=[], help="only pages under this path (repeatable)")
    p.add_argument("--front-matter", choices=("all", "skip-only", "none"), default="all",
                   help="which front matter test: keys to export; use skip-only where tests.yaml manifests "
                        "already own the scenarios, so they are not defined twice")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("strip")
    common(p, annotations=False)
    p.add_argument("--prefix", action="append", default=[])
    p.set_defaults(fn=cmd_strip)

    p = sub.add_parser("attach")
    common(p)
    p.add_argument("--out", help="write attached pages here instead of in place")
    p.add_argument("--page", action="append", help="only this page (repeatable)")
    p.add_argument("--report", help="write a JSON report of close matches and needs-update markup")
    p.add_argument("--summary", action="store_true", help="also list them in $GITHUB_STEP_SUMMARY")
    p.set_defaults(fn=cmd_attach)

    p = sub.add_parser("refresh")
    common(p)
    p.add_argument("--since", help="content commit of the last refresh (default: refresh-state.yaml)")
    p.add_argument("--report")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("slack", help="Slack payloads for a refresh report")
    p.add_argument("--report", help="the refresh --report file")
    p.add_argument("--run-url")
    p.add_argument("--failed", action="store_true", help="the refresh failed; post the failure message")
    p.set_defaults(fn=cmd_slack)

    p = sub.add_parser("annotate")
    common(p)
    p.add_argument("--page", required=True, help="repo-relative page path")
    p.add_argument("--out", required=True, help="where to write the marked-up copy")
    p.set_defaults(fn=cmd_annotate)

    p = sub.add_parser("save")
    common(p)
    p.add_argument("--page", required=True)
    p.add_argument("--edited", required=True, help="the marked-up copy from annotate")
    p.set_defaults(fn=cmd_save)

    p = sub.add_parser("roundtrip")
    common(p, annotations=False)
    p.add_argument("--prefix", action="append", default=[])
    p.set_defaults(fn=cmd_roundtrip)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
