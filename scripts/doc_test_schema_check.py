#!/usr/bin/env python3
"""'schema'-type doc test: statically validate a documented custom resource example
against the real CRD's OpenAPI schema.

Unlike the 'functional'/'live'/'credentialed' types, this never stands up a cluster
or executes anything -- it reads the CRD manifest's
`spec.versions[].schema.openAPIV3Schema` (checked into the agentgateway/agentgateway
repo) and validates the YAML a doc page shows a reader against it. This catches
renamed/removed/mistyped fields for near-zero cost, and runs on every PR regardless
of vendor credentials or live infrastructure.

A scenario reaches type 'schema' only if it declares a real custom resource (a `kind`
this script has a matching CRD schema for, e.g. AgentgatewayPolicy or
AgentgatewayBackend) in a visible, non-hidden fenced code block. Gateway API
core types (HTTPRoute, Gateway, ...) have no CRD to check here -- they're not
this checker's job, and blocks that don't parse as a K8s manifest are skipped, not
flagged.
"""

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    yaml = None

try:
    import jsonschema  # type: ignore[import-not-found]
except ModuleNotFoundError:
    jsonschema = None

from doc_test_extract import Extractor
from doc_test_run import (
    DEFAULT_OPTIONS,
    build_test_cases,
    build_test_cases_from_file,
    load_annotation_reports,
    parse_front_matter,
    unattached_for,
    write_job_summary,
    write_report,
)

logger = logging.getLogger(__name__)

# `kubectl apply -f- <<EOF ... EOF` (optionally `<<'EOF'` / `<<-EOF`) is the
# standard wrapper these docs use so the same block is both valid YAML to read
# and a pasteable terminal command. Other test types run it as-is; here we only
# want the YAML in between.
HEREDOC_RE = re.compile(r"<<-?['\"]?(\w+)['\"]?\s*\n(.*?)^\1\s*$", re.DOTALL | re.MULTILINE)


def extract_yaml_documents(block_content: str) -> List[str]:
    match = HEREDOC_RE.search(block_content)
    text = match.group(2) if match else block_content
    return [d for d in text.split("\n---\n") if d.strip()]


def _make_strict(node):
    """Recursively force `additionalProperties: false` onto every structured object
    in a CRD schema.

    Kubernetes CRD schemas leave `additionalProperties` unset by default, which
    means the API server *prunes* unknown fields at apply time rather than
    rejecting them -- and plain `jsonschema.validate()` treats unset the same way
    (permissive). Left alone, a renamed or misspelled field in a doc's example
    would silently validate, which defeats the entire point of the 'schema' type.
    This mirrors what `kubectl apply --validate=strict` enforces server-side.

    Only object nodes that declare `properties` are tightened; a node whose
    `additionalProperties` is already an explicit schema (the map-type pattern,
    e.g. `additionalProperties: {type: string}` for a `map[string]string`) or
    that opts out via `x-kubernetes-preserve-unknown-fields: true` is left as-is.
    """
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node and not node.get("x-kubernetes-preserve-unknown-fields"):
            node["additionalProperties"] = False
        for value in node.values():
            _make_strict(value)
    elif isinstance(node, list):
        for item in node:
            _make_strict(item)
    return node


def load_crd_schemas(crd_dir: Path) -> Dict[str, dict]:
    """Map CRD `kind` -> its openAPIV3Schema, scanning every CRD manifest in crd_dir."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    schemas: Dict[str, dict] = {}
    for crd_file in sorted(crd_dir.glob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(crd_file.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:
            logger.warning("Skipping unparsable CRD file %s: %s", crd_file, exc)
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "CustomResourceDefinition":
                continue
            names = doc.get("spec", {}).get("names", {})
            kind = names.get("kind")
            versions = doc.get("spec", {}).get("versions", [])
            served = next((v for v in versions if v.get("served")), versions[0] if versions else None)
            if not kind or not served:
                continue
            schema = served.get("schema", {}).get("openAPIV3Schema")
            if schema:
                schemas[kind] = _make_strict(schema)
    return schemas


def check_manifest(manifest: dict, schemas: Dict[str, dict]) -> Optional[str]:
    """Validate one parsed manifest against its CRD schema.

    Returns an error string, or None if the manifest passed (or belongs to a
    kind this checker has no schema for, which is not a failure -- it's simply
    out of scope for the 'schema' type).
    """
    kind = manifest.get("kind")
    if kind not in schemas:
        return None
    if jsonschema is None:
        raise RuntimeError("jsonschema is required. Install it with: pip install jsonschema")
    try:
        jsonschema.validate(manifest, schemas[kind])
    except jsonschema.ValidationError as exc:
        loc = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"{kind} at {loc}: {exc.message}"
    return None


def check_scenario(
    repo_root: Path,
    doc_file: Path,
    sources: List[Dict[str, str]],
    schemas: Dict[str, dict],
    docs_tests_root: Optional[Path],
    read: Optional[Set[Path]] = None,
) -> List[str]:
    """Run the 'schema' check across every source step of one test scenario.

    `read`, if given, collects every file the scenario's steps read, so the
    caller can tell whether markup that did not attach belongs to it.

    Most steps are prerequisite pages (install, gateway setup) with no CRD
    manifest to check -- that's expected, not an error. The scenario only fails
    outright if literally nothing checkable was found anywhere in the chain.
    """
    errors: List[str] = []
    checked = 0
    for src in sources:
        source_file = repo_root / src["file"]
        definition = {
            "name": "schema-check",
            "main_file": doc_file.relative_to(repo_root).as_posix(),
            "context": {"version": "main", "product": "kubernetes"},
            "options": DEFAULT_OPTIONS,
            "sources": [{"file": src["file"], "paths": [src["path"]]}],
            "output": {},
        }
        extractor = Extractor(repo_root=repo_root, definition=definition, docs_tests_root=docs_tests_root)
        extractor.walk()
        if read is not None:
            read |= extractor.read_files()
        blocks = [b for b in extractor.select_blocks() if not b.hidden and b.language in ("yaml", "yml", "sh", "bash", "shell")]

        for block in blocks:
            for doc_text in extract_yaml_documents(block.content):
                try:
                    manifest = yaml.safe_load(doc_text)
                except yaml.YAMLError:
                    continue
                if not isinstance(manifest, dict) or "kind" not in manifest:
                    continue
                checked += 1
                err = check_manifest(manifest, schemas)
                if err:
                    errors.append(f"{source_file.relative_to(repo_root).as_posix()}:{block.start_line}: {err}")

    if checked == 0:
        errors.append(
            f"{doc_file.relative_to(repo_root).as_posix()}: found no recognizable custom "
            "resource manifest to validate anywhere in this scenario's source chain"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="'schema'-type doc test: validate documented CRs against the real CRD schema, no cluster involved."
    )
    parser.add_argument("--repo-root", default=".", help="Workspace root")
    parser.add_argument(
        "--crd-dir",
        required=True,
        help="Directory of CRD YAML manifests to validate against (e.g. a checkout of "
        "agentgateway/agentgateway's controller/install/helm/agentgateway-crds/templates)",
    )
    parser.add_argument(
        "--docs-tests-root",
        default=None,
        help="Path to a docs-tests checkout, for {{< doc-test file=\"...\" >}} external content.",
    )
    parser.add_argument("--docs-glob", default="content/docs/**/*.md", help="Glob to discover markdown docs")
    parser.add_argument("--file", nargs="+", default=None, metavar="FILE", help="Only check test scenarios declared on these file(s)")
    parser.add_argument(
        "--report-file",
        default=None,
        help="Write a YAML report in the same shape doc_test_run.py writes, so it merges into the "
        "same aggregation/Slack pipeline (e.g. out/tests/generated/schema-results.yaml).",
    )
    parser.add_argument(
        "--annotation-report",
        action="append",
        default=[],
        metavar="PATH",
        help="Report from `annotations.py attach --report` (repeatable). A scenario that would "
             "select markup which did not attach is skipped with a warning; otherwise it would "
             "check fewer manifests than it claims, or fail for finding none.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)

    if yaml is None:
        logger.error("PyYAML is required. Install it with: pip install pyyaml")
        return 1
    if jsonschema is None:
        logger.error("jsonschema is required. Install it with: pip install jsonschema")
        return 1

    repo_root = Path(args.repo_root).resolve()
    crd_dir = Path(args.crd_dir).resolve()
    docs_tests_root = Path(args.docs_tests_root).resolve() if args.docs_tests_root else None
    schemas = load_crd_schemas(crd_dir)
    logger.info("Loaded %d CRD schema(s) from %s: %s", len(schemas), crd_dir, ", ".join(sorted(schemas)))

    if args.file:
        test_cases = []
        for f in args.file:
            md_file = repo_root / f if not Path(f).is_absolute() else Path(f)
            cases, _ = build_test_cases_from_file(repo_root, md_file, generated_dir=repo_root / "out/tests/generated")
            test_cases.extend(cases)
    else:
        test_cases, _, _, _ = build_test_cases(
            repo_root, args.docs_glob, generated_dir=repo_root / "out/tests/generated",
            docs_tests_root=docs_tests_root,
        )

    schema_cases = [tc for tc in test_cases if tc.type == "schema"]
    if not schema_cases:
        logger.info("No 'schema'-type scenarios found.")
        if args.report_file:
            write_report(Path(args.report_file), [], {})
        return 0

    unattached = load_annotation_reports(args.annotation_report)
    exit_code = 0
    test_results: Dict[str, Dict] = {}
    skipped: Dict[str, Dict[str, Any]] = {}
    for tc in schema_cases:
        doc_rel = tc.document.relative_to(repo_root).as_posix()
        key = f"{doc_rel}::{tc.name}"
        read: Set[Path] = set()
        errors = check_scenario(repo_root, tc.document, tc.sources, schemas, docs_tests_root, read)
        missing = unattached_for(tc, read, unattached)
        if missing:
            skipped[key] = {"markup": [
                {"page": m.page.as_posix(), "kind": m.kind, "paths": ",".join(sorted(m.selectors)), "reason": m.reason}
                for m in missing
            ]}
            logger.warning("SKIPPED: %s: %d piece(s) of its test markup did not attach (needs update)", key, len(missing))
            continue
        if errors:
            exit_code = 1
            logger.error("FAILED: %s", key)
            for err in errors:
                logger.error("  %s", err)
            test_results[key] = {"status": "failed", "checks": [], "error": "\n".join(errors), "type": "schema"}
        else:
            logger.info("PASSED: %s", key)
            test_results[key] = {"status": "passed", "checks": [], "type": "schema"}

    write_job_summary(skipped)
    if args.report_file:
        tested_documents = sorted({tc.document.relative_to(repo_root).as_posix() for tc in schema_cases})
        write_report(Path(args.report_file), tested_documents, test_results, skipped=skipped)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
