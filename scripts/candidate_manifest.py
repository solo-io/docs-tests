#!/usr/bin/env python3
"""Write the fixer agent's functional-test drafts into the manifests.

The agent never edits tests.yaml. For each candidate from
list_functional_candidates.py it writes a summary row, and this script, run
from a copy of the scripts taken before the agent started, turns the rows into
manifest edits:

    {"key": ..., "kind": "drafted", "fix": true, "reason": ..., "public_reason": ...,
     "files": [<annotation files>],
     "scenario": {"needs": [<prerequisite>, ...], "before": [{"page" | "source": ..., "path": ...}],
                  "path": "<paths value>", "assert": ["<command>", ...]}}
        -> the scenario becomes `type: [schema, functional]`, with these fields

    {"key": ..., "kind": "declined", "fix": false, "reason": ..., "public_reason": ...}
        -> the scenario keeps `type: schema` and gets `functional: "<public_reason>"`,
           so it is never a candidate again

Only the candidate's own scenario changes, and only these fields; a row that
names an unknown prerequisite, an unknown field, or a scenario that is not a
candidate stops the run. The entry is rebuilt each time from the copy in the
candidate row (`candidate.entry`), the entry before any attempt, so a second
attempt that declines a page the first attempt drafted leaves no trace of the
draft. Every manifest it changed is written to --edits-out, so
failure_fix_check.py can tell these edits from the agent's.

revert_entry() is for the publish job: a drafted test that did not pass goes
back to the entry it had, with a `functional:` reason, so it is not drafted
again every night.

`public_reason` is used, not `reason`: docs-tests is public, and a private
product's reason may describe its pages.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from doc_test_type_check import _scenario_span  # noqa: E402

FIELDS = ("needs", "before", "path", "assert")
# Field order of a written entry; anything else in the original entry follows.
ORDER = ("needs", "before", "type", "source", "page", "path", "assert", "scaffolded", "functional")


def _quote(s: str) -> str:
    return json.dumps(s)   # a JSON string is a valid YAML double-quoted scalar


def _field_lines(name: str, value) -> List[str]:
    if name == "path":
        return [f"    path: {value}"]
    if name in ("needs", "assert"):
        return [f"    {name}:"] + [f"    - {_quote(v) if name == 'assert' else v}" for v in value]
    out = [f"    {name}:"]
    for step in value:
        (k0, v0), *rest = step.items()
        out.append(f"    - {k0}: {v0}")
        out += [f"      {k}: {v}" for k, v in rest]
    return out


def entry_lines(entry: dict) -> List[str]:
    """A scenario's body, four spaces in, in ORDER."""
    out: List[str] = []
    for name in list(ORDER) + [k for k in entry if k not in ORDER]:
        if name not in entry:
            continue
        value = entry[name]
        if name == "type":
            out += [f"    type: {value}"] if isinstance(value, str) else ["    type:"] + [f"    - {v}" for v in value]
        elif name in FIELDS:
            out += _field_lines(name, value)
        elif isinstance(value, str) and name in ("scaffolded", "functional"):
            out.append(f"    {name}: {_quote(value)}")
        else:
            out += ["    " + l for l in yaml.safe_dump({name: value}, sort_keys=False).rstrip("\n").split("\n")]
    return out


def write_entry(lines: List[str], key: str, entry: dict) -> List[str]:
    start, end = _scenario_span(lines, key)
    return lines[:start + 1] + entry_lines(entry) + lines[end:]


def revert_entry(text: str, base_text: str, key: str, reason=None) -> str:
    """`text` with scenario `key` as it is in `base_text`, plus `functional: reason` when given."""
    base = (yaml.safe_load(base_text) or {}).get("scenarios", {}).get(key)
    if not isinstance(base, dict):
        raise ValueError(f"scenario `{key}` is not in the base manifest")
    entry = {k: (str(v) if k == "scaffolded" else v) for k, v in base.items()}
    if reason:
        entry["functional"] = reason
    return "\n".join(write_entry(text.split("\n"), key, entry))


def validate(scenario: dict, prerequisites: Dict[str, dict]) -> List[str]:
    errors = [f"unknown field `{k}`" for k in scenario if k not in FIELDS]
    needs = scenario.get("needs") or []
    if not isinstance(needs, list) or not all(isinstance(n, str) for n in needs):
        errors.append("`needs` must be a list of prerequisite names")
    else:
        errors += [f"`needs` names `{n}`, which is not in `prerequisites:`" for n in needs if n not in prerequisites]
    for step in scenario.get("before") or []:
        if not isinstance(step, dict) or not ({"page", "source"} & set(step)) or set(step) - {"page", "source", "path"} \
                or not all(isinstance(v, str) and v and "\n" not in v for v in step.values()):
            errors.append(f"`before` step {step!r} needs `page:` or `source:`, and optionally `path:`")
    path = scenario.get("path")
    if path is not None and (not isinstance(path, str) or not re.fullmatch(r"[\w.,-]+", path)):
        errors.append("`path` must be a paths value such as `my-feature`")
    asserts = scenario.get("assert") or []
    if not isinstance(asserts, list) or not all(isinstance(a, str) and "\n" not in a for a in asserts):
        errors.append("`assert` must be a list of one-line commands")
    return errors


def apply_rows(docs_tests_root: Path, candidates: List[dict], rows: List[dict]) -> Dict[str, List[str]]:
    """Edit the manifests in place. Returns {manifest: [change, ...]}; raises ValueError on a bad row."""
    by_key = {c["key"]: c for c in candidates if c.get("candidate")}
    edits: Dict[str, List[str]] = {}
    texts: Dict[str, List[str]] = {}
    errors: List[str] = []
    for row in rows:
        kind = row.get("kind")
        if kind not in ("drafted", "declined"):
            continue
        cand = by_key.get(row.get("key"))
        if cand is None:
            errors.append(f"{row.get('key')}: a {kind} row for something that is not a candidate")
            continue
        manifest, key = cand["candidate"]["manifest"], cand["candidate"]["scenario"]
        path = docs_tests_root / manifest
        if manifest not in texts:
            texts[manifest] = path.read_text(encoding="utf-8").split("\n")
        data = yaml.safe_load("\n".join(texts[manifest])) or {}
        lines = texts[manifest]
        entry = dict(cand["candidate"].get("entry") or (data.get("scenarios") or {}).get(key) or {})
        if not entry:
            errors.append(f"{row['key']}: scenario `{key}` is not in {manifest}")
            continue
        if kind == "declined":
            reason = (row.get("public_reason") or "").strip()
            if not reason or "\n" in reason:
                errors.append(f"{row['key']}: a declined row needs a one-line public_reason")
                continue
            entry["functional"] = reason
            edits.setdefault(manifest, []).append(f"{key}: kept schema only ({reason})")
        else:
            scenario = row.get("scenario") or {}
            bad = validate(scenario, data.get("prerequisites") or {})
            if bad:
                errors += [f"{row['key']}: {b}" for b in bad]
                continue
            entry["type"] = ["schema", "functional"]
            entry.update({k: v for k, v in scenario.items() if v})
            edits.setdefault(manifest, []).append(f"{key}: added a functional test")
        lines = write_entry(lines, key, entry)
        texts[manifest] = lines
    if errors:
        raise ValueError("\n".join(errors))
    for manifest, lines in texts.items():
        text = "\n".join(lines)
        yaml.safe_load(text)   # still a manifest
        (docs_tests_root / manifest).write_text(text, encoding="utf-8")
    return edits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs-tests-root", required=True)
    ap.add_argument("--failures", required=True, help="the failure list the agent worked from, candidates included")
    ap.add_argument("--summary", required=True, help="the agent's fix-summary.json")
    ap.add_argument("--edits-out", required=True, help="write {manifest: [change, ...]} here")
    args = ap.parse_args(argv)
    candidates = json.loads(Path(args.failures).read_text(encoding="utf-8"))
    try:
        rows = (json.loads(Path(args.summary).read_text(encoding="utf-8")).get("failures") or [])
        edits = apply_rows(Path(args.docs_tests_root), candidates, rows)
    except (OSError, ValueError) as e:
        for line in str(e).splitlines():
            print(f"::error::{line}")
        return 1
    Path(args.edits_out).write_text(json.dumps(edits, indent=2) + "\n", encoding="utf-8")
    for manifest, changes in edits.items():
        for c in changes:
            print(f"{manifest}: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
