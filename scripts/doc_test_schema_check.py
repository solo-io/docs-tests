#!/usr/bin/env python3
"""'schema'-type doc test: statically validate the configuration a doc page shows
a reader against the real schema it has to satisfy.

Unlike the 'functional'/'live'/'credentialed' types, this never stands up a cluster
or executes anything. It catches renamed/removed/mistyped fields for near-zero
cost, and runs on every PR regardless of vendor credentials or live infrastructure.

What gets checked, and against what:

- Custom resources (agentgateway's own, and Gateway API kinds such as HTTPRoute
  or Gateway): the CRD's `spec.versions[].schema.openAPIV3Schema` for the
  manifest's `apiVersion`, from every CRD manifest passed with --crd-dir.
- Core Kubernetes kinds (Service, Deployment, Secret, ...): the Kubernetes
  OpenAPI schema, through kubeconform, when --kubeconform is given.
- Standalone config files (`binds:`, `routes:`, `llm:`, ...) on standalone
  pages: agentgateway's `schema/config.json`, when --config-schema is given.

Blocks that don't parse as YAML are skipped, not flagged. They don't count
toward a pass, though: a scenario in which nothing was validated fails, rather
than passing with nothing checked. Helm values and CRD CEL rules
(`x-kubernetes-validations`) are not checked; see solo-io/docs-tests#19.

Each version tree should be checked against the release it documents, not the
default branch. Pass `--crd-dir latest=DIR --crd-dir main=DIR` (and the same for
--config-schema); see --help.
"""

import argparse
import json
import logging
import multiprocessing
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    yaml = None

try:
    import jsonschema  # type: ignore[import-not-found]
except ModuleNotFoundError:
    jsonschema = None

from doc_test_extract import Extractor, resolve_docs_tests_root
from doc_test_run import (
    DEFAULT_OPTIONS,
    _mode_segment,
    build_test_cases,
    build_test_cases_from_file,
    infer_version_from_sources,
    load_annotation_reports,
    parse_front_matter,
    unattached_for,
    write_job_summary,
    write_report,
)

logger = logging.getLogger(__name__)

# `kubectl apply -f- <<EOF ... EOF` (optionally `<<'EOF'` / `<<-EOF`) is the
# standard wrapper these docs use so the same block is both valid YAML to read
# and a pasteable terminal command. Standalone pages write their config the same
# way, with a redirect after the delimiter: `cat <<'EOF' > config.yaml` or
# `cat > config.yaml << 'EOF'`. Other test types run it as-is; here we only want
# the YAML in between, from every heredoc in the block.
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)^\s*\1\s*$", re.DOTALL | re.MULTILINE)

# (API group, kind) -> {API version: validator}. The group keeps Gateway API's
# Gateway apart from Istio's.
CrdValidators = Dict[Tuple[str, str], Dict[str, Any]]


@dataclass
class Schemas:
    """Everything one version tree is validated against."""
    crds: CrdValidators = field(default_factory=dict)
    config: Optional[Any] = None   # validator for the standalone config file


def extract_yaml_documents(block_content: str) -> List[str]:
    heredocs = [m.group(2) for m in HEREDOC_RE.finditer(block_content)]
    text = "\n---\n".join(heredocs) if heredocs else block_content
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


def _validator(schema: dict):
    """A reusable validator. Building one per manifest re-checks the schema every time."""
    if jsonschema is None:
        raise RuntimeError("jsonschema is required. Install it with: pip install jsonschema")
    return jsonschema.validators.validator_for(schema)(schema)


def load_crd_schemas(path: Path) -> CrdValidators:
    """Map (group, kind) -> {served version: validator}, from every CRD manifest
    in `path` (a directory of *.yaml files, or one file, such as Gateway API's
    experimental-install.yaml)."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    schemas: CrdValidators = {}
    files = [path] if path.is_file() else sorted(path.glob("*.yaml"))
    for crd_file in files:
        try:
            docs = list(yaml.safe_load_all(crd_file.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:
            logger.warning("Skipping unparsable CRD file %s: %s", crd_file, exc)
            continue
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("kind") != "CustomResourceDefinition":
                continue
            spec = doc.get("spec", {})
            kind = spec.get("names", {}).get("kind")
            group = spec.get("group", "")
            if not kind:
                continue
            for v in spec.get("versions", []):
                schema = v.get("schema", {}).get("openAPIV3Schema")
                if v.get("served") and schema:
                    schemas.setdefault((group, kind), {})[v["name"]] = _validator(_make_strict(schema))
    return schemas


def load_config_schema(path: Path):
    """The standalone config file's JSON schema (agentgateway's schema/config.json).

    Used as published, not made strict: unlike a CRD's, this schema already says
    where unknown fields are rejected.
    """
    return _validator(json.loads(path.read_text(encoding="utf-8")))


# `$VAR` / `${VAR}`. In an unquoted heredoc the shell fills these in before the
# resource reaches the API server, so `hostname: ${AUTH0_DOMAIN}` is a real
# hostname by then. Its pattern, format, or type can't be judged from the page.
SHELL_VAR_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*")


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(SHELL_VAR_RE.search(value))


def _value_at(doc: Any, path: List[Any]) -> Any:
    for part in path:
        try:
            doc = doc[int(part)] if isinstance(doc, list) else doc[part]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return doc


def _first_error(validator, instance) -> Optional[str]:
    errors = [e for e in validator.iter_errors(instance) if not _is_placeholder(e.instance)]
    err = jsonschema.exceptions.best_match(errors)
    if err is None:
        return None
    loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
    return f"at {loc}: {err.message}"


def check_manifest(manifest: dict, crds: CrdValidators) -> Optional[str]:
    """Validate one parsed manifest against its CRD schema, for its apiVersion.

    Returns an error string, or None if the manifest passed. The caller makes
    sure the manifest's (group, kind) is in `crds`.
    """
    kind = manifest.get("kind")
    group, _, version = str(manifest.get("apiVersion", "")).rpartition("/")
    versions = crds[(group, kind)]
    if version not in versions:
        return (f"{kind}: apiVersion {manifest.get('apiVersion')!r} is not served by its CRD "
                f"(serves {', '.join(f'{group}/{v}' for v in sorted(versions))})")
    err = _first_error(versions[version], manifest)
    return f"{kind} {err}" if err else None


# A line that is only `...` or `# ...` stands in for fields an example leaves out.
ELISION_RE = re.compile(r"^\s*(#\s*)?\.\.\.\s*$", re.MULTILINE)
JSON_TYPES = {"array": list, "object": dict}


def is_config_file(doc: Any, config_validator, text: str = "") -> bool:
    """A whole standalone config file: a mapping with no `kind`, whose top-level
    keys are all config sections (binds, routes, llm, ...).

    Pages also show things that look like one but are not, and are skipped:
    - a fragment of one section, which has other top-level keys, or uses a
      section name for something else: a route's `policies:` mapping looks like
      the top-level `policies`, which is a list, and Docker Compose's
      `services:` mapping looks like the top-level `services` list;
    - an example that elides fields with `# ...`;
    - Helm values for the standalone chart, which nest the whole config under
      `config:`.
    """
    if not isinstance(doc, dict) or not doc or "kind" in doc or ELISION_RE.search(text):
        return False
    sections = config_validator.schema.get("properties", {})
    if not all(isinstance(k, str) and k in sections for k in doc):
        return False
    for key, value in doc.items():
        want = JSON_TYPES.get(sections[key].get("type"))
        if want is not None and value is not None and not isinstance(value, want):
            return False
    nested = doc.get("config")
    return not (isinstance(nested, dict) and any(k in sections for k in nested))


class Kubeconform:
    """Validate core Kubernetes kinds with kubeconform, one call per scenario.

    `-strict` rejects unknown fields, the same as _make_strict does for CRDs.
    `-ignore-missing-schemas` reports a kind it has no schema for (any CRD it
    was not given) as skipped, which the caller counts as unchecked.
    """

    def __init__(self, binary: str, kubernetes_version: Optional[str]):
        self.binary = binary
        self.kubernetes_version = kubernetes_version
        self._caches: Dict[int, str] = {}

    @property
    def cache(self) -> str:
        """A schema cache per process: parallel workers must not read a schema
        file another worker is still writing."""
        pid = os.getpid()
        if pid not in self._caches:
            self._caches[pid] = tempfile.mkdtemp(prefix="kubeconform-cache-")
        return self._caches[pid]

    def validate(self, manifests: List[dict]) -> List[Tuple[str, Optional[str]]]:
        """One (status, error) per manifest, in order. Status is "valid", "invalid"
        or "skipped"; a kubeconform error (for example, a schema it could not
        download) counts as invalid, so a broken run cannot pass."""
        if not manifests:
            return []
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i, m in enumerate(manifests):
                f = Path(tmp) / f"{i}.yaml"
                f.write_text(yaml.safe_dump(m), encoding="utf-8")
                files.append(str(f))
            cmd = [self.binary, "-strict", "-ignore-missing-schemas", "-output", "json", "-verbose",
                   "-cache", self.cache]
            if self.kubernetes_version:
                cmd += ["-kubernetes-version", self.kubernetes_version]
            proc = subprocess.run(cmd + files, capture_output=True, text=True)
            try:
                resources = json.loads(proc.stdout or "{}").get("resources") or []
            except json.JSONDecodeError:
                return [("invalid", f"kubeconform failed: {proc.stderr.strip() or proc.stdout.strip()}")] * len(manifests)
            by_file = {Path(r.get("filename", "")).name: r for r in resources}
        results: List[Tuple[str, Optional[str]]] = []
        for i, m in enumerate(manifests):
            r = by_file.get(f"{i}.yaml")
            status = (r or {}).get("status", "")
            if status == "statusValid":
                results.append(("valid", None))
            elif status == "statusSkipped" or r is None and proc.returncode == 0:
                results.append(("skipped", None))
            else:
                errs = [e for e in (r or {}).get("validationErrors") or []
                        if not _is_placeholder(_value_at(m, [p for p in str(e.get("path", "")).split("/") if p]))]
                if status == "statusInvalid" and (r or {}).get("validationErrors") and not errs:
                    results.append(("valid", None))   # every error was on a shell variable
                    continue
                msg = "; ".join(f"at {e.get('path') or '(root)'}: {e.get('msg')}" for e in errs) or (r or {}).get("msg") \
                    or proc.stderr.strip() or "kubeconform reported no result"
                results.append(("invalid", f"{m.get('kind')} {msg}"))
        return results


def parse_versioned(values: List[str]) -> Dict[Optional[str], List[Path]]:
    """Map link version (e.g. "latest", "main") -> paths.

    Each value is either `PATH`, used for every version, or `VERSION=PATH`, used
    only for scenarios in that version tree. The None key holds the bare paths.
    """
    paths: Dict[Optional[str], List[Path]] = {}
    for value in values:
        version, sep, path = value.partition("=")
        if not sep or "/" in version:
            version, path = None, value
        paths.setdefault(version, []).append(Path(path).resolve())
    return paths


# Links never contribute blocks to a schema check (select_blocks takes them only
# from a step's own pages, and read_files leaves link-only pages out), and
# following them was over 80% of the run time.
SCHEMA_OPTIONS = {**DEFAULT_OPTIONS, "follow_internal_links": False}
LANGS = ("yaml", "yml", "sh", "bash", "shell")


@dataclass
class StepResult:
    """One source step, checked once however many scenarios share it."""
    errors: List[str] = field(default_factory=list)
    checked: int = 0
    unchecked_kinds: Set[str] = field(default_factory=set)
    unparsed: int = 0
    read: Set[Path] = field(default_factory=set)


# Set in main(), before the pool forks, from --upstream-root and
# --upstream-versions: where a {{< rebase >}} shell's content comes from. Empty
# for a site with no shells.
_UPSTREAM: Dict[str, Any] = {}


def _extractor(repo_root: Path, main_rel: str, sources: List[Dict[str, Any]], version: str, mode: str,
               docs_tests_root: Optional[Path]) -> Extractor:
    definition = {
        "name": "schema-check",
        "main_file": main_rel,
        "context": {"version": version, "product": mode, "conditions": _UPSTREAM.get("conditions", [])},
        "options": SCHEMA_OPTIONS,
        "sources": sources,
        "output": {},
    }
    extractor = Extractor(repo_root=repo_root, definition=definition, docs_tests_root=docs_tests_root,
                          upstream_root=_UPSTREAM.get("root"), upstream_version_for=_UPSTREAM.get("versions"))
    extractor.walk()
    return extractor


def check_step(repo_root: Path, file: str, path: str, schemas: Schemas, docs_tests_root: Optional[Path],
               version: str, mode: str, page_wide: bool, kubeconform: Optional[Kubeconform] = None) -> StepResult:
    """Check one source step: the blocks tagged `path` on `file`, plus, when
    `page_wide`, every other visible block on that page."""
    res = StepResult()
    source_file = repo_root / file
    extractor = _extractor(repo_root, file, [{"file": file, "paths": [path]}], version, mode, docs_tests_root)
    res.read = extractor.read_files()
    blocks = [b for b in extractor.select_blocks() if not b.hidden and b.language in LANGS]
    # The badge says the resources and config files ON THIS PAGE were
    # checked, so check every visible block on the page itself, not only the
    # ones tagged for this scenario. A reader's copy is often untagged: on
    # standalone pages the functional test runs a hidden copy of the config
    # with --validate-only. Prerequisite pages (install, gateway setup) are
    # left to their own scenarios, so one bad block fails one page.
    if page_wide:
        selected = {(b.file_path, b.start_line) for b in blocks}
        result = extractor.file_cache.get(source_file.resolve())
        for b in (result.code_blocks if result else []):
            if not b.hidden and b.language in LANGS and (b.file_path, b.start_line) not in selected:
                selected.add((b.file_path, b.start_line))
                blocks.append(b)

    core: List[Tuple[str, dict]] = []   # (location, manifest) for kubeconform
    for block in blocks:
        loc = f"{source_file.relative_to(repo_root).as_posix()}:{block.start_line}"
        for doc_text in extract_yaml_documents(block.content):
            try:
                doc = yaml.safe_load(doc_text)
            except yaml.YAMLError:
                res.unparsed += 1
                continue
            # Only standalone pages have config files; a Helm values file on
            # a Kubernetes page can share a top-level key such as `config`.
            if mode == "standalone" and schemas.config is not None and is_config_file(doc, schemas.config, doc_text):
                res.checked += 1
                err = _first_error(schemas.config, doc)
                if err:
                    res.errors.append(f"{loc}: config file {err}")
                continue
            if not isinstance(doc, dict) or "kind" not in doc:
                continue
            group = str(doc.get("apiVersion", "")).rpartition("/")[0]
            if (group, doc["kind"]) in schemas.crds:
                res.checked += 1
                err = check_manifest(doc, schemas.crds)
                if err:
                    res.errors.append(f"{loc}: {err}")
            elif kubeconform is not None:
                core.append((loc, doc))
            else:
                res.unchecked_kinds.add(str(doc["kind"]))

    for (loc, doc), (status, err) in zip(core, kubeconform.validate([d for _, d in core]) if core else []):
        if status == "skipped":
            res.unchecked_kinds.add(str(doc["kind"]))
            continue
        res.checked += 1
        if err:
            res.errors.append(f"{loc}: {err}")
    return res


def page_read_files(repo_root: Path, doc_rel: str, version: str, mode: str,
                    docs_tests_root: Optional[Path]) -> Set[Path]:
    """The scenario's own page and whatever it reuses, for unattached_for."""
    return _extractor(repo_root, doc_rel, [], version, mode, docs_tests_root).read_files()


def _units(repo_root: Path, doc_file: Path, sources: List[Dict[str, str]], version: str):
    """The cache keys of one scenario's work: its page, then each source step."""
    doc_rel = doc_file.relative_to(repo_root).as_posix()
    mode = _mode_segment(doc_rel) or "kubernetes"
    yield ("page", doc_rel, version, mode)
    for src in sources:
        page_wide = (repo_root / src["file"]).resolve() == doc_file.resolve()
        yield ("step", src["file"], src["path"], version, mode, page_wide)


def _run_unit(unit, repo_root: Path, schemas: Schemas, docs_tests_root: Optional[Path],
              kubeconform: Optional[Kubeconform]):
    if unit[0] == "page":
        _, doc_rel, version, mode = unit
        return page_read_files(repo_root, doc_rel, version, mode, docs_tests_root)
    _, file, path, version, mode, page_wide = unit
    return check_step(repo_root, file, path, schemas, docs_tests_root, version, mode, page_wide, kubeconform)


def check_scenario(
    repo_root: Path,
    doc_file: Path,
    sources: List[Dict[str, str]],
    schemas: Schemas,
    docs_tests_root: Optional[Path],
    read: Optional[Set[Path]] = None,
    version: str = "main",
    kubeconform: Optional[Kubeconform] = None,
    cache: Optional[Dict[Any, Any]] = None,
) -> Tuple[List[str], int]:
    """Run the 'schema' check across every source step of one test scenario.

    Returns the errors and the number of manifests and config files validated.
    `version` is the link version ("latest", "main") the extractor resolves
    version-gated content against. `read`, if given, collects every file the
    scenario's steps read, so the caller can tell whether markup that did not
    attach belongs to it. `cache` holds results of steps already checked, keyed
    by _units(); a step shared by many scenarios (install, gateway setup) is
    checked once. It must only ever see one set of schemas.

    Most steps are prerequisite pages (install, gateway setup) with nothing to
    check -- that's expected, not an error. The scenario fails if nothing
    anywhere in the chain was validated: a scenario of only kinds this run has
    no schema for would otherwise pass without checking anything.
    """
    cache = {} if cache is None else cache
    errors: List[str] = []
    checked = 0
    unchecked_kinds: Set[str] = set()
    unparsed = 0
    for unit in _units(repo_root, doc_file, sources, version):
        if unit not in cache:
            cache[unit] = _run_unit(unit, repo_root, schemas, docs_tests_root, kubeconform)
        result = cache[unit]
        if unit[0] == "page":
            if read is not None:
                read |= result
            continue
        if read is not None:
            read |= result.read
        errors.extend(result.errors)
        checked += result.checked
        unchecked_kinds |= result.unchecked_kinds
        unparsed += result.unparsed

    if unparsed:
        logger.debug("%s: %d YAML document(s) did not parse and were not checked",
                     doc_file.relative_to(repo_root).as_posix(), unparsed)
    if checked == 0:
        found = f" (found only {', '.join(sorted(unchecked_kinds))}, which have no schema here)" if unchecked_kinds else ""
        errors.append(
            f"{doc_file.relative_to(repo_root).as_posix()}: found no recognizable custom "
            f"resource manifest or config file to validate anywhere in this scenario's source chain{found}"
        )
    return errors, checked


def load_schemas(crd_paths: Dict[Optional[str], List[Path]],
                 config_paths: Dict[Optional[str], List[Path]]) -> Dict[Optional[str], Schemas]:
    """One Schemas per version key. Bare paths apply to every version; a
    VERSION= path wins over a bare one for the same kind."""
    for paths in list(crd_paths.values()) + list(config_paths.values()):
        for p in paths:
            if not p.exists():
                raise ValueError(f"{p} does not exist")
    for version, paths in config_paths.items():
        if len(paths) > 1:
            raise ValueError(f"--config-schema given twice for {version or 'every version'}")

    base = Schemas()
    for p in crd_paths.get(None, []):
        base.crds.update(load_crd_schemas(p))
    if None in config_paths:
        base.config = load_config_schema(config_paths[None][0])
    by_version: Dict[Optional[str], Schemas] = {}
    if None in crd_paths or None in config_paths:
        by_version[None] = base
    for version in {v for v in list(crd_paths) + list(config_paths) if v is not None}:
        s = Schemas(crds=dict(base.crds), config=base.config)
        for p in crd_paths.get(version, []):
            s.crds.update(load_crd_schemas(p))
        if version in config_paths:
            s.config = load_config_schema(config_paths[version][0])
        by_version[version] = s
    return by_version


# Set in main() before the pool forks, so workers inherit the loaded
# validators instead of pickling them.
_POOL: Dict[str, Any] = {}


def _pool_unit(unit):
    version = unit[2] if unit[0] == "page" else unit[3]
    schemas = _POOL["schemas"].get(version, _POOL["schemas"].get(None))
    return unit, _run_unit(unit, _POOL["repo_root"], schemas, _POOL["docs_tests_root"], _POOL["kubeconform"])


def run_units(units: List[Any], jobs: int) -> Dict[Any, Any]:
    """Every unit's result, `jobs` at a time. Units are independent; chunksize 1
    keeps a slow page from holding up a queue of others behind it."""
    if jobs <= 1 or len(units) < 2:
        return dict(_pool_unit(u) for u in units)
    # fork: workers start with the loaded schemas, and need no __main__ guard.
    with multiprocessing.get_context("fork").Pool(jobs) as pool:
        return dict(pool.imap_unordered(_pool_unit, units, chunksize=1))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="'schema'-type doc test: validate documented resources and config against "
        "their real schemas, no cluster involved."
    )
    parser.add_argument("--repo-root", default=".", help="Workspace root")
    parser.add_argument(
        "--crd-dir",
        required=True,
        action="append",
        metavar="[VERSION=]PATH",
        help="Directory of CRD YAML manifests, or a single YAML file of them, to validate "
        "against (e.g. agentgateway/agentgateway's "
        "controller/install/helm/agentgateway-crds/templates, or Gateway API's "
        "experimental-install.yaml). Repeatable. VERSION=PATH applies to scenarios in that "
        "link-version tree only (e.g. latest=agw-latest/..., main=agw-main/...), so each tree "
        "is checked against the release it documents. A bare PATH applies to every version. "
        "A scenario whose version has neither fails.",
    )
    parser.add_argument(
        "--config-schema",
        action="append",
        default=[],
        metavar="[VERSION=]FILE",
        help="JSON schema for the standalone config file (agentgateway/agentgateway's "
        "schema/config.json), checked on standalone pages only. Same VERSION= form as --crd-dir.",
    )
    parser.add_argument(
        "--kubeconform",
        default=None,
        metavar="PATH",
        help="kubeconform binary. When given, core Kubernetes kinds (Service, Deployment, ...) "
        "are validated against the Kubernetes OpenAPI schema; otherwise they are not checked.",
    )
    parser.add_argument(
        "--kubernetes-version",
        default=None,
        help="Kubernetes version for kubeconform's schemas, e.g. 1.36.0 (default: kubeconform's).",
    )
    parser.add_argument(
        "--docs-tests-root",
        default=None,
        help="Path to a docs-tests checkout, for {{< doc-test file=\"...\" >}} external content.",
    )
    parser.add_argument(
        "--upstream-root",
        default=None,
        help="Checkout of the repo this site rebases its content from, as in doc_test_run.py. "
        "Without it a {{< rebase >}} shell has nothing to check.",
    )
    parser.add_argument(
        "--upstream-versions",
        default=None,
        metavar="JSON",
        help="Which upstream version directory feeds each of this site's versions, as in "
        "doc_test_run.py (e.g. '{\"latest\": \"main\"}'). Required with --upstream-root.",
    )
    parser.add_argument(
        "--condition",
        action="append",
        default=[],
        metavar="NAME",
        help="Also match conditional-text gates that name NAME, as in doc_test_run.py. Repeatable.",
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
    parser.add_argument(
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="Steps to check in parallel (default: the number of CPUs). 1 runs everything in this process.",
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
    docs_tests_root = resolve_docs_tests_root(args.docs_tests_root)
    _UPSTREAM.clear()
    _UPSTREAM["conditions"] = args.condition
    if args.upstream_root:
        if not args.upstream_versions:
            # Same rule as doc_test_run.py: a shell with no version map expands
            # to nothing, and would fail for the wrong reason.
            logger.error("--upstream-root was given without --upstream-versions.")
            return 1
        try:
            _UPSTREAM["versions"] = json.loads(args.upstream_versions)
        except json.JSONDecodeError as exc:
            logger.error("--upstream-versions is not valid JSON: %s", exc)
            return 1
        _UPSTREAM["root"] = Path(args.upstream_root).resolve()
    try:
        schemas_by_version = load_schemas(parse_versioned(args.crd_dir), parse_versioned(args.config_schema))
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    for version, s in sorted(schemas_by_version.items(), key=lambda kv: kv[0] or ""):
        logger.info("Schemas for %s: %d CRD kind(s) (%s); standalone config schema: %s",
                    version or "every version", len(s.crds), ", ".join(sorted(k for _, k in s.crds)),
                    "yes" if s.config is not None else "no")
    kubeconform = Kubeconform(args.kubeconform, args.kubernetes_version) if args.kubeconform else None

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

    # Check every distinct step once, in parallel, then assemble each scenario
    # from the results. Scenarios share most of their steps (install, gateway
    # setup), so this is far less work than checking scenario by scenario.
    units: Dict[Any, None] = {}
    for tc in schema_cases:
        version = infer_version_from_sources(tc.sources, "main")
        if schemas_by_version.get(version, schemas_by_version.get(None)) is not None:
            units.update(dict.fromkeys(_units(repo_root, tc.document, tc.sources, version)))
    _POOL.update(repo_root=repo_root, schemas=schemas_by_version, docs_tests_root=docs_tests_root,
                 kubeconform=kubeconform)
    logger.info("Checking %d distinct step(s) for %d scenario(s), %d at a time",
                len(units), len(schema_cases), max(1, args.jobs))
    cache = run_units(list(units), args.jobs)

    unattached = load_annotation_reports(args.annotation_report)
    exit_code = 0
    test_results: Dict[str, Dict] = {}
    skipped: Dict[str, Dict[str, Any]] = {}
    for tc in schema_cases:
        doc_rel = tc.document.relative_to(repo_root).as_posix()
        key = f"{doc_rel}::{tc.name}"
        read: Set[Path] = set()
        # The same lookup doc_test_run.py makes for functional runs: the version
        # dir of the scenario's sources, so a latest/ page resolves its gates
        # and picks its schemas as latest, not main.
        version = infer_version_from_sources(tc.sources, "main")
        schemas = schemas_by_version.get(version, schemas_by_version.get(None))
        if schemas is None:
            errors = [f"{doc_rel}: no CRD schemas for version '{version}'; pass --crd-dir {version}=PATH"]
            checked = 0
        else:
            errors, checked = check_scenario(repo_root, tc.document, tc.sources, schemas, docs_tests_root,
                                             read, version, kubeconform, cache)
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
            logger.info("PASSED: %s (%s, %d resource(s) checked)", key, version, checked)
            test_results[key] = {"status": "passed", "checks": [], "type": "schema"}

    write_job_summary(skipped)
    if args.report_file:
        tested_documents = sorted({tc.document.relative_to(repo_root).as_posix() for tc in schema_cases})
        write_report(Path(args.report_file), tested_documents, test_results, skipped=skipped)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
