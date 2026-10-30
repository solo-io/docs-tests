#!/usr/bin/env python3
"""Check that each scenario's `type:` still fits the page it tests.

A page gets its test type when someone writes the scenario: `schema` for a page
that shows resources, `functional` for one with steps to run, `skip:` for one
with neither. Pages are rewritten afterwards and the type is not. Nothing else
notices, because the stale test still passes:

- A `schema` scenario counts resources across its whole prerequisite chain, so
  a page whose own YAML was removed still passes on the Gateway from the
  gateway setup page.
- A `functional` scenario whose page lost its commands runs the prerequisites
  and its hidden checks, and asserts on nothing the page says.
- A page under `skip:` that gained resources is never checked at all.

This looks only at the scenario's own page (its `source:` or `page:` step, not
`needs:` or `before:`), in every tested version root it resolves in, and
reports:

  error    schema-empty         `schema`, but the page shows no resource or config file
  error    functional-empty     `functional`, but the page has
                                no visible yaml or shell block left
  error    skip-checkable       under `skip:`, but the page shows resources
  warning  schema-unreadable    `schema`, but none of the page's resources parse
  warning  could-be-functional  `schema` only or `skip:`, but the page has commands to run

Errors already in the product's baseline (type-check-baseline.yaml) are
reported as `baselined` and do not fail the run.

A change is only suggested when it holds in EVERY version root the page is in:
a page that lost its YAML on main but not on latest keeps its schema test until
the release, and a page that gained YAML on main only is not moved off `skip:`,
because a scenario runs in both roots and would fail on latest.

`--apply` makes the fixes for the three errors in tests.yaml: it drops the type
that no longer fits (and moves a scenario left with none to `skip:`), and turns
a checkable `skip:` page into a `schema` scenario. Adding a `functional`
scenario is writing a test, which needs prerequisites and assertions, so
could-be-functional is reported only. Edits are made line by line, so comments
survive, and the result is re-parsed and compared with the intended change
before anything is written.

Run it after `annotations.py attach`, like the schema check: which blocks a
scenario selects comes from the annotations.
"""
import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from doc_test_extract import resolve_docs_tests_root  # noqa: E402
from doc_test_run import TESTED_VERSIONS, _resolve_manifest_ref, load_test_manifests  # noqa: E402
import doc_test_schema_check  # noqa: E402
from doc_test_schema_check import (  # noqa: E402
    HEREDOC_RE, LANGS, _extractor, extract_yaml_documents, is_config_file, load_config_schema,
)

logger = logging.getLogger(__name__)

# Every type that runs the page. `schema` is the only other type (see
# doc_test_run.TEST_TYPES).
RUN_TYPES = ("functional",)
OWN_KEYS = ("source", "page")
# Without --config-schema, a standalone config file is recognized by a key only
# a whole config file has at its top level.
CONFIG_MARKERS = {"binds", "llm", "mcp"}
KIND_LINE = re.compile(r"^kind:\s*\S", re.MULTILINE)


@dataclass
class Facts:
    """What one page shows, in one version root."""
    resources: int = 0          # visible manifests (with a `kind`) and whole config files on the page
    selected: int = 0           # visible blocks the step selects: the page's own part of the test
    page_commands: int = 0      # visible blocks anywhere on the page with a command to run
    visible: int = 0            # visible yaml and shell blocks on the page, whatever they hold
    unreadable: int = 0         # of `resources`, ones with a `kind:` line that do not parse as YAML


def _runnable(content: str) -> bool:
    """A block with at least one command outside its heredocs. Pages fence
    `kubectl apply -f- <<EOF` as yaml, so a yaml block can be one."""
    body = HEREDOC_RE.sub("\n", content)
    return any(line.strip() and not line.lstrip().startswith("#") for line in body.splitlines())


def _is_resource(doc: Any, text: str, mode: str, config_validator) -> bool:
    if isinstance(doc, dict) and "kind" in doc and "apiVersion" in doc:
        return True
    if mode != "standalone" or not isinstance(doc, dict) or "kind" in doc:
        return False
    if config_validator is not None:
        return is_config_file(doc, config_validator, text)
    return bool(CONFIG_MARKERS & set(doc))


def page_facts(repo_root: Path, file: str, path: Optional[str], version: str, mode: str,
               docs_tests_root: Path, config_validator=None) -> Facts:
    """Classify the visible blocks of `file`, and those its step selects."""
    extractor = _extractor(repo_root, file, [{"file": file, "paths": [path or "all"]}], version, mode,
                           docs_tests_root)
    facts = Facts()
    result = extractor.file_cache.get((repo_root / file).resolve())
    page_blocks = [b for b in (result.code_blocks if result else []) if not b.hidden and b.language in LANGS]
    facts.visible = len(page_blocks)
    for block in page_blocks:
        for text in extract_yaml_documents(block.content):
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError:
                # Still a resource the page shows; the schema check just cannot
                # read it (for example `\\$` escaped for an unquoted heredoc).
                if KIND_LINE.search(text):
                    facts.resources += 1
                    facts.unreadable += 1
                continue
            if _is_resource(doc, text, mode, config_validator):
                facts.resources += 1
        if _runnable(block.content) and not _all_yaml(block.content):
            facts.page_commands += 1
    facts.selected = sum(1 for b in extractor.select_blocks() if not b.hidden)
    return facts


def _all_yaml(content: str) -> bool:
    """A block that is a YAML document, not commands: a config file or a bare
    manifest with no `kubectl apply` around it."""
    try:
        return isinstance(yaml.safe_load(content), (dict, list))
    except yaml.YAMLError:
        return False


@dataclass
class Finding:
    manifest: str
    kind: str
    severity: str
    ref: Dict[str, str]                     # {"source": ...} or {"page": ...}
    message: str
    scenario: Optional[str] = None          # manifest key; None for a skip: entry
    types: List[str] = field(default_factory=list)
    versions: List[str] = field(default_factory=list)
    fix: Optional[str] = None
    applied_as: Optional[str] = None        # set by --apply: the scenario a skip-checkable page became


def _types(entry: Dict[str, Any]) -> List[str]:
    raw = entry.get("type", "functional")
    return [t for t in (raw if isinstance(raw, list) else [raw]) if isinstance(t, str) and t]


def _own_ref(entry: Dict[str, Any]) -> Optional[Dict[str, str]]:
    for key in OWN_KEYS:
        if entry.get(key):
            return {key: str(entry[key])}
    return None


def _facts_by_version(repo_root: Path, mpath: Path, mode: str, content_root: str, ref: Dict[str, str],
                      path: Optional[str], reverse_index, docs_tests_root: Path, config_validator,
                      cache: Dict[Any, Facts]) -> Dict[str, Facts]:
    out: Dict[str, Facts] = {}
    # A site that rebases another (the enterprise hub) can only resolve the
    # versions its upstream map names, as in the schema check.
    upstream = doc_test_schema_check._UPSTREAM.get("versions")
    for version in TESTED_VERSIONS:
        if upstream is not None and version not in upstream:
            continue
        if not (repo_root / content_root / mode / version).is_dir():
            continue
        try:
            resolved = _resolve_manifest_ref(ref, mode, version, reverse_index, mpath, "type-check", content_root)
        except RuntimeError as exc:
            logger.warning("%s", exc)
            continue
        if not resolved or not (repo_root / resolved).is_file():
            continue
        key = (resolved, path, version, mode)
        if key not in cache:
            cache[key] = page_facts(repo_root, resolved, path, version, mode, docs_tests_root, config_validator)
        out[version] = cache[key]
    return out


def check_manifest(repo_root: Path, mpath: Path, mode: str, data: Dict[str, Any], reverse_index,
                   docs_tests_root: Path, config_validator=None) -> List[Finding]:
    content_root = str(data.get("content_root") or "content/docs").strip("/")
    rel = mpath.relative_to(docs_tests_root).as_posix() if mpath.is_relative_to(docs_tests_root) else str(mpath)
    cache: Dict[Any, Facts] = {}
    findings: List[Finding] = []

    for key, entry in (data.get("scenarios") or {}).items():
        ref = _own_ref(entry) if isinstance(entry, dict) else None
        if ref is None:
            continue   # only prerequisite steps: no page of its own to judge
        types = _types(entry)
        by_version = _facts_by_version(repo_root, mpath, mode, content_root, ref, entry.get("path"),
                                       reverse_index, docs_tests_root, config_validator, cache)
        if not by_version:
            continue
        versions = sorted(by_version)
        no_resources = all(f.resources == 0 for f in by_version.values())
        # Standalone functional tests often run a hidden copy of the page's
        # config fragments, so selecting no visible block is normal. Nothing
        # left to run is a page with no visible yaml or shell block at all.
        no_commands = all(f.selected == 0 and f.visible == 0 for f in by_version.values())
        has_page_commands = all(f.page_commands > 0 for f in by_version.values())
        all_resources = all(f.resources > 0 for f in by_version.values())
        base = dict(manifest=rel, ref=ref, scenario=str(key), types=types, versions=versions)

        if "schema" in types and no_resources:
            findings.append(Finding(kind="schema-empty", severity="error", **base, message=(
                "type `schema`, but the page shows no resource or config file; the check passes only "
                "on its prerequisites' resources")))
        elif "schema" in types and all(f.resources == f.unreadable for f in by_version.values()):
            findings.append(Finding(kind="schema-unreadable", severity="warning", **base, message=(
                "type `schema`, but no resource on the page parses as YAML (often `\\$` in a heredoc), "
                "so the check passes only on its prerequisites' resources")))
        run = [t for t in types if t in RUN_TYPES]
        if run and no_commands:
            findings.append(Finding(kind="functional-empty", severity="error", **base, message=(
                f"type `{run[0]}`, but the page has no visible yaml or shell block left; only "
                "prerequisites and hidden checks run")))
        if types == ["schema"] and not no_resources and has_page_commands:
            findings.append(Finding(kind="could-be-functional", severity="warning", **base, message=(
                "type `schema` only, but the page has commands to run; a `functional` test may fit")))
        elif types == ["schema"] and no_resources and has_page_commands:
            findings.append(Finding(kind="could-be-functional", severity="warning", **base, message=(
                "the page has commands to run; a `functional` test may fit")))
        # A run type dropped for having nothing to run may still leave a page
        # worth a schema check.
        if run and no_commands and all_resources and "schema" not in types:
            findings[-1].fix = "schema"

    for entry in data.get("skip") or []:
        ref = _own_ref(entry) if isinstance(entry, dict) else None
        if ref is None:
            continue   # `dir:` covers generated pages, which nobody types by hand
        by_version = _facts_by_version(repo_root, mpath, mode, content_root, ref, None,
                                       reverse_index, docs_tests_root, config_validator, cache)
        if by_version and all(f.resources > 0 for f in by_version.values()):
            findings.append(Finding(manifest=rel, kind="skip-checkable", severity="error", ref=ref,
                                    versions=sorted(by_version), message=(
                "under `skip:`, but the page shows resources a `schema` test can check")))
        elif by_version and all(f.page_commands > 0 for f in by_version.values()):
            findings.append(Finding(manifest=rel, kind="could-be-functional", severity="warning", ref=ref,
                                    versions=sorted(by_version), message=(
                "under `skip:`, but the page has commands to run; a `functional` test may fit")))
    return findings


# ---- --apply: edit tests.yaml in place, line by line --------------------------------

def _block_end(lines: List[str], start: int, indent: int) -> int:
    """Index after the block starting at `start`: the next non-blank line at or
    below `indent`. A comment at that indent belongs to what follows it."""
    i = start + 1
    while i < len(lines):
        s = lines[i]
        if s.strip() and len(s) - len(s.lstrip()) <= indent:
            break
        i += 1
    while i > start + 1 and not lines[i - 1].strip():
        i -= 1
    return i


def _section(lines: List[str], name: str) -> Optional[Tuple[int, int]]:
    """A top-level key's lines. Its list items can sit at column 0 (`- page:`),
    so it ends at the next top-level key, less the comments in front of it."""
    for i, s in enumerate(lines):
        if s.rstrip() == f"{name}:":
            end = next((j for j in range(i + 1, len(lines)) if re.match(r"^[^\s#-]", lines[j])), len(lines))
            while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
                end -= 1
            return i, end
    return None


def _scenario_span(lines: List[str], key: str) -> Tuple[int, int]:
    sec = _section(lines, "scenarios")
    if sec:
        for i in range(sec[0] + 1, sec[1]):
            if re.match(rf"^  {re.escape(key)}:\s*$", lines[i]) or re.match(rf"^  ['\"]{re.escape(key)}['\"]:\s*$", lines[i]):
                return i, _block_end(lines, i, 2)
    raise ValueError(f"scenario `{key}` not found")


def _type_lines(types: List[str]) -> List[str]:
    if len(types) == 1:
        return [f"    type: {types[0]}"]
    return ["    type:"] + [f"    - {t}" for t in types]


def set_type(lines: List[str], key: str, types: List[str]) -> List[str]:
    start, end = _scenario_span(lines, key)
    body = lines[start + 1:end]
    i = next((n for n, s in enumerate(body) if re.match(r"^    type:", s)), None)
    if i is None:
        body = _type_lines(types) + body
    else:
        j = i + 1
        if body[i].rstrip() == "    type:":
            while j < len(body) and re.match(r"^    - ", body[j]):
                j += 1
        body = body[:i] + _type_lines(types) + body[j:]
    return lines[:start + 1] + body + lines[end:]


def remove_scenario(lines: List[str], key: str) -> List[str]:
    start, end = _scenario_span(lines, key)
    while lines[start - 1].startswith("  #"):   # its own comment goes with it
        start -= 1
    return lines[:start] + lines[end:]


def _skip_item_line(ref: Dict[str, str]) -> str:
    (k, v), = ref.items()
    return f"- {k}: {v}"


def remove_skip(lines: List[str], ref: Dict[str, str]) -> List[str]:
    sec = _section(lines, "skip")
    want = _skip_item_line(ref)
    if sec:
        for i in range(sec[0] + 1, sec[1]):
            if lines[i].rstrip() == want:
                j = i + 1
                while j < sec[1] and lines[j].startswith("  ") and not lines[j].startswith("- "):
                    j += 1
                # A comment right above an entry explains that entry; it goes too.
                while i > sec[0] + 1 and lines[i - 1].lstrip().startswith("#"):
                    i -= 1
                return lines[:i] + lines[j:]
    raise ValueError(f"skip entry `{want}` not found")


def add_skip(lines: List[str], ref: Dict[str, str]) -> List[str]:
    sec = _section(lines, "skip")
    if sec is None:
        at = _section(lines, "scenarios")[0]
        return lines[:at] + ["skip:", _skip_item_line(ref)] + lines[at:]
    return lines[:sec[1]] + [_skip_item_line(ref)] + lines[sec[1]:]


def add_scenario(lines: List[str], key: str, ref: Dict[str, str]) -> List[str]:
    sec = _section(lines, "scenarios")
    (k, v), = ref.items()
    block = [f"  {key}:", "    type: schema", f"    {k}: {v}", "    path: all"]
    keys = [(i, m.group(1)) for i in range(sec[0] + 1, sec[1])
            if (m := re.match(r"^  ([^\s#'\"][^:]*):\s*$", lines[i]))]
    at = next((i for i, existing in keys if existing > key), sec[1])
    return lines[:at] + block + lines[at:]


def _new_key(ref: Dict[str, str], taken) -> str:
    stem = Path(next(iter(ref.values()))).with_suffix("")
    stem = stem.parent.name if stem.name == "_index" else stem.name
    key = f"{stem}-schema"
    n = 2
    while key in taken:
        key, n = f"{stem}-schema-{n}", n + 1
    return key


def apply_fixes(text: str, findings: List[Finding]) -> Tuple[str, List[str]]:
    """Return the edited manifest text and a line per change made.

    Every edit is mirrored on the parsed manifest, and the edited text must
    parse to exactly that, or nothing is returned edited.
    """
    data = yaml.safe_load(text)
    expected = json.loads(json.dumps(data))
    lines = text.splitlines()
    changes: List[str] = []
    by_scenario: Dict[str, List[Finding]] = {}
    for f in findings:
        if f.scenario and f.severity == "error":
            by_scenario.setdefault(f.scenario, []).append(f)

    for key, fs in by_scenario.items():
        entry = expected["scenarios"][key]
        types = _types(entry)
        kinds = {f.kind for f in fs}
        keep = [t for t in types
                if not (t == "schema" and "schema-empty" in kinds)
                and not (t in RUN_TYPES and "functional-empty" in kinds)]
        if any(f.fix == "schema" for f in fs) and "schema" not in keep:
            keep = ["schema"] + keep
        if keep:
            entry["type"] = keep[0] if len(keep) == 1 else keep
            lines = set_type(lines, key, keep)
            changes.append(f"{key}: type {'/'.join(types)} -> {'/'.join(keep)}")
            continue
        ref = _own_ref(entry)
        del expected["scenarios"][key]
        lines = remove_scenario(lines, key)
        others = any(_own_ref(e) == ref for e in expected["scenarios"].values() if isinstance(e, dict))
        if not others and ref not in [_own_ref(s) for s in expected.get("skip") or [] if isinstance(s, dict)]:
            expected.setdefault("skip", []).append(ref)
            lines = add_skip(lines, ref)
            changes.append(f"{key}: removed; {_skip_item_line(ref)} added to skip")
        else:
            changes.append(f"{key}: removed (the page has another scenario)")

    for f in findings:
        if f.kind != "skip-checkable":
            continue
        key = _new_key(f.ref, expected["scenarios"])
        expected["skip"] = [s for s in expected["skip"] if not isinstance(s, dict) or _own_ref(s) != f.ref]
        lines = remove_skip(lines, f.ref)
        (k, v), = f.ref.items()
        expected["scenarios"][key] = {"type": "schema", k: v, "path": "all"}
        lines = add_scenario(lines, key, f.ref)
        f.applied_as = key
        changes.append(f"{key}: added as `schema`; {_skip_item_line(f.ref)} removed from skip")

    out = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if yaml.safe_load(out) != expected:
        raise ValueError("the edited manifest does not parse to the intended change; nothing written")
    return out, changes


# ---- baseline -----------------------------------------------------------------------
#
# Findings that predate this check are a backlog, not drift: failing CI on them
# would turn every consumer red at once. The baseline lists them, one line each,
# and a run fails only on an error that is not in it. Fix a backlog entry with
# --apply --no-baseline, then regenerate the baseline with --write-baseline.

def finding_key(f: Finding) -> str:
    return f"{f.manifest} {f.kind} {f.scenario or 'skip'} {next(iter(f.ref.values()))}"


def load_baseline(path: Optional[str]) -> set:
    if not path or not Path(path).is_file():
        return set()
    return set(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or [])


def write_baseline(path: str, findings: List[Finding]) -> None:
    keys = sorted({finding_key(f) for f in findings if f.severity == "error"})
    header = ("# Doc test type check findings that predate the check (doc_test_type_check.py).\n"
              "# A run fails only on an error not listed here. Regenerate with --write-baseline.\n")
    Path(path).write_text(header + (yaml.safe_dump(keys, width=1000) if keys else "[]\n"), encoding="utf-8")


# ---- CLI ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Check each doc test scenario's type against its page.")
    parser.add_argument("--repo-root", default=".", help="Content repo, with annotations attached")
    parser.add_argument("--docs-tests-root", default=None)
    parser.add_argument("--product", action="append", default=[],
                        help="Only this product's manifests (products/<product>/). Repeatable.")
    parser.add_argument("--upstream-root", default=None,
                        help="Where {{< rebase >}} shells' content comes from, as in doc_test_schema_check.py")
    parser.add_argument("--upstream-versions", default=None,
                        help="JSON map of this site's versions to upstream version directories. Only these "
                        "versions are checked. Required with --upstream-root.")
    parser.add_argument("--condition", action="append", default=[], metavar="NAME",
                        help="Also match conditional-text gates that name NAME. Repeatable.")
    parser.add_argument("--config-schema", default=None,
                        help="agentgateway schema/config.json, to recognize standalone config files exactly")
    parser.add_argument("--baseline", default=None,
                        help="Known findings (default: products/<product>/type-check-baseline.yaml for one --product)")
    parser.add_argument("--no-baseline", action="store_true", help="Treat every finding as new")
    parser.add_argument("--write-baseline", action="store_true", help="Write the current errors to the baseline")
    parser.add_argument("--report", default=None, help="Write findings as JSON")
    parser.add_argument("--summary", default=None, help="Append a Markdown summary (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--apply", action="store_true", help="Fix the new errors in tests.yaml")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s", stream=sys.stderr)

    from reuse_consumers import build_reverse_index
    repo_root = Path(args.repo_root).resolve()
    docs_tests_root = resolve_docs_tests_root(args.docs_tests_root)
    config_validator = load_config_schema(Path(args.config_schema)) if args.config_schema else None
    # The schema check's extractor reads these, so a shell resolves the same way here.
    doc_test_schema_check._UPSTREAM["conditions"] = args.condition
    if args.upstream_root:
        if not args.upstream_versions:
            parser.error("--upstream-root needs --upstream-versions")
        doc_test_schema_check._UPSTREAM["root"] = Path(args.upstream_root).resolve()
        doc_test_schema_check._UPSTREAM["versions"] = json.loads(args.upstream_versions)
    reverse_index = build_reverse_index(repo_root)
    baseline_path = args.baseline or (
        str(docs_tests_root / "products" / args.product[0] / "type-check-baseline.yaml")
        if len(args.product) == 1 else None)
    baseline = set() if args.no_baseline or args.write_baseline else load_baseline(baseline_path)

    findings: List[Finding] = []
    applied: Dict[str, List[str]] = {}
    for mpath, mode, data in load_test_manifests(docs_tests_root):
        if args.product and mpath.parent.parent.name not in args.product:
            continue
        found = check_manifest(repo_root, mpath, mode, data, reverse_index, docs_tests_root, config_validator)
        for f in found:
            if f.severity == "error" and finding_key(f) in baseline:
                f.severity = "baselined"
        findings.extend(found)
        if args.apply and not args.write_baseline and any(f.severity == "error" for f in found):
            text, changes = apply_fixes(mpath.read_text(encoding="utf-8"), [f for f in found if f.severity == "error"])
            mpath.write_text(text, encoding="utf-8")
            applied[found[0].manifest] = changes

    if args.write_baseline:
        if not baseline_path:
            parser.error("--write-baseline needs --baseline or exactly one --product")
        write_baseline(baseline_path, findings)
        logger.info("Wrote %s", baseline_path)

    errors = [f for f in findings if f.severity == "error"]
    for f in findings:
        if f.severity == "baselined":
            continue
        where = f"{f.manifest}: {f.scenario or 'skip'} ({next(iter(f.ref.values()))})"
        (logger.error if f.severity == "error" else logger.warning)("%s: %s: %s", where, f.kind, f.message)
    stale = baseline - {finding_key(f) for f in findings}
    for key in sorted(stale):
        logger.info("Baseline entry no longer found, remove it: %s", key)
    for manifest, changes in applied.items():
        for c in changes:
            logger.info("APPLIED %s: %s", manifest, c)
    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("error", "warning", "baselined")}
    logger.info("%d error(s), %d warning(s), %d baselined", counts["error"], counts["warning"], counts["baselined"])

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(
            {"findings": [dict(f.__dict__, key=finding_key(f)) for f in findings], "applied": applied, "stale_baseline": sorted(stale)},
            indent=2) + "\n", encoding="utf-8")
    if args.summary:
        shown = [f for f in findings if f.severity != "baselined"]
        with open(args.summary, "a", encoding="utf-8") as out:
            out.write(f"### Test type check\n\n{counts['error']} error(s), {counts['warning']} warning(s), "
                      f"{counts['baselined']} baselined\n\n")
            if shown:
                out.write("| Severity | Manifest | Scenario | Page | Finding |\n|---|---|---|---|---|\n")
                for f in shown:
                    out.write(f"| {f.severity} | {f.manifest} | {f.scenario or '`skip:`'} | "
                              f"`{next(iter(f.ref.values()))}` | {f.message} |\n")
    return 1 if errors and not args.apply else 0


if __name__ == "__main__":
    sys.exit(main())
