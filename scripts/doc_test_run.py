#!/usr/bin/env python3

import argparse
import json
import logging
import os
import hashlib
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    yaml = None

from doc_test_extract import Extractor, resolve_docs_tests_root

logger = logging.getLogger(__name__)

DEFAULT_OPTIONS = {
    "follow_reuse": True,
    "follow_include": True,
    "follow_internal_links": True,
    "skip_tabs_without_paths": True,
    "max_depth": 8,
}


@dataclass
class TestCase:
    document: Path
    name: str
    sources: List[Dict[str, Any]]
    script_path: Path
    manifest_path: Path
    type: str = "functional"


def parse_front_matter(markdown_path: Path) -> Dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    text = markdown_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    front_matter = match.group(1)
    data = yaml.safe_load(front_matter) or {}
    if not isinstance(data, dict):
        return {}
    return data


def sanitize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def infer_version_from_sources(sources: List[Dict[str, Any]], fallback: str) -> str:
    """Extract the link-version token (e.g. 'latest', 'main') from source file paths.

    Source files live under paths like:
      content/docs/kubernetes/latest/install/helm.md
      content/docs/kubernetes/main/security/cors.md

    The segment after the product directory (kubernetes/standalone) is the
    link version used inside {{< version include-if="..." >}} blocks.
    """
    pattern = re.compile(r"(?:kubernetes|standalone)/([^/]+)/")
    for src in sources:
        file_path = src.get("file", "")
        m = pattern.search(file_path)
        if m:
            return m.group(1)
    return fallback


def version_path_tokens(doc_rel_path: str) -> Dict[str, str]:
    """Build the path-substitution tokens for a page's `test:` metadata.

    Derived from the declaring page's repo-relative path
    (e.g. "content/docs/standalone/main/configuration/backends.md"):

      ${version}     -> the version dir segment, e.g. "main" / "latest"
      ${versionRoot} -> the prefix up to and including the version dir,
                        e.g. "content/docs/standalone/main"

    These let `file:` values reference paths without hardcoding the version
    directory, which rotates every release. Resolution is anchored to the page
    that declares the test, so a page copied from main/ to latest/ needs no
    metadata edit. Entries that intentionally target another version just write
    the literal path (no token). Returns an empty dict for paths that don't
    match the content/docs/<section>/<version>/ layout, leaving `file:`
    unchanged.

    The ${...} syntax (not {...}) is deliberate: a YAML value starting with
    "{" is parsed as a flow mapping, so a leading {versionRoot} would be a
    parse error unless quoted. A leading "$" is a plain scalar, so ${versionRoot}
    is valid unquoted at the start of a `file:` value.
    """
    parts = doc_rel_path.replace("\\", "/").split("/")
    try:
        idx = parts.index("docs")
        section = parts[idx + 1]
        version = parts[idx + 2]
    except (ValueError, IndexError):
        return {}
    if section not in ("kubernetes", "standalone"):
        return {}
    version_root = "/".join(parts[: idx + 3])
    return {"${versionRoot}": version_root, "${version}": version}


def build_test_cases_from_file(
    repo_root: Path,
    md_file: Path,
    generated_dir: Path,
    filter_test_name: Optional[str] = None,
) -> Tuple[List[TestCase], List[str]]:
    """Build test cases from a single markdown file, optionally filtered to one test name."""
    test_cases: List[TestCase] = []
    tested_documents: List[str] = []

    if not md_file.is_file():
        return test_cases, tested_documents

    metadata = parse_front_matter(md_file)
    tests = metadata.get("test")
    if tests == "skip":
        tested_documents.append(md_file.relative_to(repo_root).as_posix())
        return test_cases, tested_documents
    if not isinstance(tests, dict) or not tests:
        return test_cases, tested_documents

    rel_doc = md_file.relative_to(repo_root).as_posix()
    tested_documents.append(rel_doc)

    doc_slug = sanitize_name(str(md_file.relative_to(repo_root).with_suffix("")))
    for test_name, entries in tests.items():
        if not isinstance(test_name, str) or not test_name:
            continue
        # A multi-type scenario's individual TestCases are named "test_name::type"
        # (see effective_name below) -- filter_test_name may be either the bare
        # scenario name (selects every declared type) or one specific "name::type".
        # The exact effective_name match happens per-type below, once test_types is
        # known; this is just a cheap early skip for scenarios that can't match at all.
        if filter_test_name and filter_test_name != test_name and not filter_test_name.startswith(f"{test_name}::"):
            continue

        # A scenario is either a bare list of {file, path} steps (legacy form,
        # implicitly "functional" -- every test written before this typing existed
        # already applies real config and asserts on real behavior, which is what
        # "functional" means) or a dict with an explicit `type:` alongside `steps:`.
        # `type:` also accepts a list (e.g. `[schema, functional]`) so one scenario's
        # steps can be validated more than one way without duplicating the whole
        # `steps:` chain into a second, separate scenario -- see test_types below.
        raw_type: Any = "functional"
        if isinstance(entries, dict):
            raw_type = entries.get("type", "functional")
            entries = entries.get("steps")
        if not isinstance(entries, list):
            continue

        test_types = raw_type if isinstance(raw_type, list) else [raw_type]
        test_types = [t for t in test_types if isinstance(t, str) and t]
        if not test_types:
            continue

        sources: List[Dict[str, Any]] = []
        tokens = version_path_tokens(rel_doc)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source_path = entry.get("path")
            if not source_path:
                continue
            # `file` defaults to the page that declares the test, and supports
            # ${version}/${versionRoot} placeholders resolved against that page's
            # path. This lets entries reference a path without hardcoding the
            # version dir (which rotates every release), so a page copied from
            # main/ to latest/ needs no metadata edit -- the version context is
            # still inferred from the resolved path. Entries that intentionally
            # point at another version (e.g. a latest/ page reusing main/'s code
            # blocks) just write the literal path with no token.
            source_file = entry.get("file") or rel_doc
            for token, value in tokens.items():
                source_file = source_file.replace(token, value)
            source: Dict[str, object] = {"file": source_file, "path": source_path}
            # Optional: names the hidden assertion content directly (docs-tests-relative
            # paths, in order) instead of requiring an inline {{< doc-test file="..." >}}
            # shortcode in the page body for this path. See doc_test_extract.py's
            # external_asserts_by_file / _synthesize_assert_blocks.
            assert_files = entry.get("assert")
            if assert_files:
                source["assert"] = assert_files
            sources.append(source)

        if not sources:
            continue

        # A single-type scenario keeps its plain name (and therefore its existing
        # script/manifest filenames and report key) unchanged, for backward
        # compatibility with every scenario declared before list-form `type:`
        # existed. A multi-type scenario gets one TestCase per type, distinguished
        # by a `name::type` suffix -- sanitize_name() turns e.g. "rewrite::schema"
        # into "rewrite-schema", the exact filename shape already used by the
        # existing hand-written `<name>-schema` scenarios, so this is consistent
        # with the established convention rather than inventing a new one.
        for test_type in test_types:
            effective_name = test_name if len(test_types) == 1 else f"{test_name}::{test_type}"
            if filter_test_name and filter_test_name not in (test_name, effective_name):
                continue
            test_slug = sanitize_name(effective_name)
            script_name = f"{doc_slug}-{test_slug}.sh"
            manifest_name = f"{doc_slug}-{test_slug}.manifest.json"

            test_cases.append(
                TestCase(
                    document=md_file,
                    name=effective_name,
                    sources=sources,
                    script_path=generated_dir / script_name,
                    manifest_path=generated_dir / manifest_name,
                    type=test_type,
                )
            )

    return test_cases, sorted(set(tested_documents))


def _version_key(doc_path: str) -> str:
    """Extract 'product/version' from a path like content/docs/kubernetes/main/..."""
    parts = doc_path.replace("\\", "/").split("/")
    try:
        idx = parts.index("docs")
        return "/".join(parts[idx + 1 : idx + 3])
    except (ValueError, IndexError):
        return "unknown"


# The only version directories that are tested. The frozen release trees
# (1.0.x, 1.3.x, …) pin chart versions that no longer install: Kubernetes
# tightened its CEL validation cost estimator, so the v1.3.1 CRDs are now
# rejected with "estimated rule cost exceeds budget", and no edit to the
# guides can change that. Both discovery paths have to agree on this, or a
# PR touching every version root pulls in hundreds of tests that a scheduled
# run never executes and that cannot be made to pass.
TESTED_VERSIONS = ("latest", "main")


def _mode_segment(doc_path: str) -> str:
    """Extract the deployment mode ('kubernetes', 'standalone') from a doc path."""
    parts = doc_path.replace("\\", "/").split("/")
    try:
        idx = parts.index("docs")
    except ValueError:
        return ""
    return parts[idx + 1] if len(parts) > idx + 1 else ""


def _version_segment(doc_path: str) -> str:
    """Extract the version directory ('main', '1.3.x') from a doc path.

    Returns "" for paths outside the content/docs/<product>/<version>/ layout.
    """
    parts = doc_path.replace("\\", "/").split("/")
    try:
        idx = parts.index("docs")
    except ValueError:
        return ""
    return parts[idx + 2] if len(parts) > idx + 2 else ""



# ---------------------------------------------------------------------------
# Manifest discovery
#
# A scenario can be declared here, in this repo, instead of in the consuming
# page's front matter. See products/<product>/<mode>/tests.yaml.
#
# A manifest entry is keyed by the assets/ file its page reuses, not by the
# page's own path, and ONE entry covers every live version root. That is the
# point: over the seven weeks after agentgateway/website#920 opened,
# content/docs/ churned 89% while assets/agw-docs/ churned 2%, and a page-keyed
# definition would have needed fixing on nearly every page.
#
# Manifests and front matter coexist. A scenario named by a manifest WINS: the
# front-matter copy of the same (mode, name) is ignored rather than producing a
# second, duplicate test case. That is what makes the migration incremental --
# an area can move to a manifest, be verified, and have its front matter
# stripped later as pure cleanup, with no window where tests run twice.
MANIFEST_RELATIVE_GLOB = "products/*/*/tests.yaml"


def _manifest_mode(manifest_path: Path) -> str:
    """products/<product>/<mode>/tests.yaml -> <mode> (e.g. 'kubernetes')."""
    return manifest_path.parent.name


def load_test_manifests(docs_tests_root: Optional[Path]) -> List[Tuple[Path, str, Dict[str, Any]]]:
    """[(path, mode, parsed)] for every manifest in the docs-tests checkout."""
    if yaml is None or docs_tests_root is None:
        return []
    found = []
    for mpath in sorted(docs_tests_root.glob(MANIFEST_RELATIVE_GLOB)):
        try:
            data = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise RuntimeError(f"{mpath}: could not be parsed as YAML: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("scenarios"), dict):
            raise RuntimeError(f"{mpath}: no `scenarios:` mapping.")
        found.append((mpath, _manifest_mode(mpath), data))
    return found


def _resolve_manifest_ref(
    ref: Dict[str, Any],
    mode: str,
    version: str,
    reverse_index: Dict[str, Set[str]],
    manifest_path: Path,
    scenario: str,
    content_root: str = "content/docs",
) -> Optional[str]:
    """A manifest step -> the content path it means in THIS version root.

    Returns None when the ref names a page that does not exist in this root,
    which is normal: a guide added after a release is absent from the older
    tree, and that scenario simply does not apply there.
    """
    root_prefix = f"{content_root}/{mode}/{version}/"
    if "page" in ref:
        return root_prefix + str(ref["page"]).lstrip("/")
    source = ref.get("source")
    if not source:
        raise RuntimeError(
            f"{manifest_path}: scenario '{scenario}' has a step with neither "
            f"`source:` nor `page:`."
        )
    key = f"assets/{str(source).lstrip('/')}"
    candidates = sorted(c for c in reverse_index.get(key, ()) if c.startswith(root_prefix))
    if not candidates:
        return None
    if len(candidates) > 1:
        # Ambiguity is a real authoring error, not something to guess at: two
        # pages in one version root reusing the same snippet means `source:`
        # cannot identify which one the scenario is about.
        raise RuntimeError(
            f"{manifest_path}: scenario '{scenario}' step source '{source}' resolves to "
            f"{len(candidates)} pages under {root_prefix}: {', '.join(candidates)}. "
            f"Name the page directly with `page:` instead."
        )
    return candidates[0]


def build_test_cases_from_manifests(
    repo_root: Path,
    docs_tests_root: Optional[Path],
    generated_dir: Path,
    filter_test_name: Optional[str] = None,
) -> Tuple[List[TestCase], Set[Tuple[str, str]], List[str]]:
    """Test cases declared in docs-tests manifests.

    Also returns the (page, scenario-name) pairs the manifests actually resolved,
    so the front-matter pass can skip exactly those and nothing else.

    Keyed by resolved PAGE rather than by (mode, name): a scenario name is not
    unique within a mode either. `llm-model-headers` is declared both on
    traffic-management/transformations/llm-model-headers.md and on
    llm/transformations.md, and `tracing` on both a transformations page and
    observability/traces/setup.md. Claiming by name alone silently dropped the
    other page's test.
    """
    manifests = load_test_manifests(docs_tests_root)
    if not manifests:
        return [], set(), []

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from reuse_consumers import build_reverse_index
    except Exception as exc:
        raise RuntimeError(f"manifest discovery needs reuse_consumers.py: {exc}") from exc
    reverse_index = build_reverse_index(repo_root)

    test_cases: List[TestCase] = []
    claimed: Set[Tuple[str, str]] = set()
    tested_documents: List[str] = []

    for mpath, mode, data in manifests:
        # Consuming sites do not all lay content out the same way: one puts it at
        # content/docs/<mode>/<version>/, another at
        # content/<lang>/<product>/<mode>/<version>/. The manifest says which,
        # rather than this guessing from the tree.
        content_root = str(data.get("content_root") or "content/docs").strip("/")
        prerequisites = data.get("prerequisites") or {}
        for key, entry in (data.get("scenarios") or {}).items():
            if not isinstance(entry, dict):
                raise RuntimeError(f"{mpath}: scenario '{key}' is not a mapping.")
            # Scenario names are not unique within a mode (llm-model-headers is
            # declared on a traffic-management page and on an llm page), but a
            # manifest is a mapping. The key only has to be unique; `name:` is
            # the scenario's real name, which test selection and results use.
            name = str(entry.get("name") or key)
            if filter_test_name and filter_test_name != name and not filter_test_name.startswith(f"{name}::"):
                continue

            refs: List[Dict[str, Any]] = []
            for need in entry.get("needs") or []:
                if need not in prerequisites:
                    raise RuntimeError(
                        f"{mpath}: scenario '{name}' needs '{need}', which is not in `prerequisites:`."
                    )
                refs.append(prerequisites[need])
            refs.extend(entry.get("before") or [])
            own = {k: v for k, v in entry.items() if k in ("source", "page", "path", "assert")}
            if own:
                refs.append(own)
            if not refs:
                raise RuntimeError(f"{mpath}: scenario '{name}' has no steps.")

            raw_type = entry.get("type", "functional")
            test_types = raw_type if isinstance(raw_type, list) else [raw_type]
            test_types = [t for t in test_types if isinstance(t, str) and t]
            if not test_types:
                raise RuntimeError(f"{mpath}: scenario '{name}' has an empty `type:`.")

            for version in TESTED_VERSIONS:
                if not (repo_root / content_root / mode / version).is_dir():
                    continue
                sources: List[Dict[str, Any]] = []
                incomplete = False
                for ref in refs:
                    resolved = _resolve_manifest_ref(
                        ref, mode, version, reverse_index, mpath, name, content_root
                    )
                    # Absent from this root (added after a release, or disabled
                    # by renaming to .txt): the scenario does not apply here.
                    if resolved is None or not (repo_root / resolved).is_file():
                        incomplete = True
                        break
                    src: Dict[str, Any] = {"file": resolved, "path": ref.get("path")}
                    if ref.get("assert"):
                        src["assert"] = list(ref["assert"])
                    sources.append(src)
                if incomplete or not sources:
                    continue

                document = repo_root / sources[-1]["file"]
                rel_doc = sources[-1]["file"]
                claimed.add((rel_doc, name))
                tested_documents.append(rel_doc)
                doc_slug = sanitize_name(str(Path(rel_doc).with_suffix("")))
                for test_type in test_types:
                    effective_name = name if len(test_types) == 1 else f"{name}::{test_type}"
                    if filter_test_name and filter_test_name not in (name, effective_name):
                        continue
                    test_slug = sanitize_name(effective_name)
                    test_cases.append(
                        TestCase(
                            document=document,
                            name=effective_name,
                            sources=sources,
                            script_path=generated_dir / f"{doc_slug}-{test_slug}.sh",
                            manifest_path=generated_dir / f"{doc_slug}-{test_slug}.manifest.json",
                            type=test_type,
                        )
                    )

        # `skip:` names pages that deliberately have no test (a section index,
        # a concept page), the manifest form of front matter `test: skip`. They
        # count as covered, so coverage measures pages someone decided about.
        # `dir:` skips every page under a folder, for generated pages such as
        # a CLI reference, where new pages appear without anyone editing here.
        for ref in data.get("skip") or []:
            if not isinstance(ref, dict):
                raise RuntimeError(f"{mpath}: each `skip:` entry needs `page:`, `source:` or `dir:`.")
            for version in TESTED_VERSIONS:
                if not (repo_root / content_root / mode / version).is_dir():
                    continue
                if "dir" in ref:
                    folder = repo_root / content_root / mode / version / str(ref["dir"]).strip("/")
                    tested_documents.extend(
                        p.relative_to(repo_root).as_posix() for p in folder.rglob("*.md")
                    )
                    continue
                resolved = _resolve_manifest_ref(ref, mode, version, reverse_index, mpath, "skip", content_root)
                if resolved and (repo_root / resolved).is_file():
                    tested_documents.append(resolved)

    return test_cases, claimed, sorted(set(tested_documents))


def build_test_cases(
    repo_root: Path,
    docs_glob: str,
    generated_dir: Path,
    docs_tests_root: Optional[Path] = None,
) -> Tuple[List[TestCase], List[str], Dict[str, int], int]:
    test_cases: List[TestCase] = []
    tested_documents: List[str] = []
    total_by_version: Dict[str, int] = {}
    total_documents = 0

    manifest_cases, claimed, manifest_docs = build_test_cases_from_manifests(
        repo_root, docs_tests_root, generated_dir
    )
    test_cases.extend(manifest_cases)
    tested_documents.extend(manifest_docs)

    for md_file in sorted(repo_root.glob(docs_glob)):
        rel = md_file.relative_to(repo_root).as_posix()
        if _version_segment(rel) not in TESTED_VERSIONS:
            continue
        vk = _version_key(rel)
        total_by_version[vk] = total_by_version.get(vk, 0) + 1
        total_documents += 1
        cases, docs = build_test_cases_from_file(repo_root, md_file, generated_dir)
        # A manifest declaration wins over the front-matter copy of the SAME
        # scenario on the SAME page, so a converted area does not run twice
        # while its front matter is still present. Page-scoped because a
        # scenario name identifies nothing on its own: it repeats across modes
        # (csrf, extproc, direct-response) and across pages within one mode
        # (llm-model-headers, tracing).
        cases = [c for c in cases if (rel, c.name.split("::")[0]) not in claimed]
        test_cases.extend(cases)
        tested_documents.extend(docs)

    return test_cases, sorted(set(tested_documents)), total_by_version, total_documents


def generate_script_and_manifest(
    repo_root: Path,
    definition: Dict,
    script_path: Path,
    manifest_path: Path,
    docs_tests_root: Optional[Path] = None,
    upstream_root: Optional[Path] = None,
    upstream_version_for: Optional[Dict[str, str]] = None,
) -> Set[Path]:
    """Write the scenario's script and manifest; return every file it read."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    extractor = Extractor(
        repo_root=repo_root,
        definition=definition,
        docs_tests_root=docs_tests_root,
        upstream_root=upstream_root,
        upstream_version_for=upstream_version_for,
    )
    extractor.walk()

    blocks = extractor.select_blocks()
    test_includes = extractor.select_test_includes()
    script = extractor.build_script(blocks, test_includes)
    manifest = extractor.build_manifest(blocks, test_includes)

    script_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return extractor.read_files()


@dataclass
class UnattachedMarkup:
    page: Path            # absolute
    selectors: Set[str]
    kind: str
    reason: str


def load_annotation_reports(paths: List[str]) -> List[UnattachedMarkup]:
    """Read `annotations.py attach --report` files: the markup that did not attach.

    Such markup can make a scenario test less than it claims.
    """
    unattached: List[UnattachedMarkup] = []
    for path in paths:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        repo = Path(report["repo"])
        for row in report.get("changed", []):
            if row["state"] == "needs-update" and row["kind"] in ("tag", "hidden"):
                unattached.append(UnattachedMarkup(
                    (repo / row["page"]).resolve(),
                    {x.strip() for x in row["label"].split(",") if x.strip()},
                    row["kind"], row.get("reason", ""),
                ))
    return unattached


def unattached_for(test_case: "TestCase", read: Set[Path], unattached: List[UnattachedMarkup]) -> List[UnattachedMarkup]:
    """Markup the scenario would have selected, on a page it read, that did not attach."""
    selectors = {x.strip() for src in test_case.sources for x in str(src.get("path") or "").split(",") if x.strip()}
    return [u for u in unattached if u.page in read and u.selectors & selectors]


def write_job_summary(skipped: Dict[str, Dict[str, Any]]) -> None:
    """List skipped scenarios in the GitHub job summary, if there is one.

    The changed tested blocks themselves are listed once by
    `annotations.py attach --summary`; this runs once per scenario in a shard.
    """
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary or not skipped:
        return
    lines = [
        "| Skipped scenario (needs update) | What did not attach |", "| --- | --- |",
    ]
    for key, info in sorted(skipped.items()):
        what = "; ".join(f"{m['kind']} `{m['paths']}` on {m['page']}" for m in info["markup"])
        lines.append(f"| `{key}` | {what} |")
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


def run_command(command: List[str], cwd: Path) -> Tuple[int, str]:
    logger.debug("$ %s", " ".join(command))

    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output_lines: List[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            output_lines.append(line)
            logger.debug("%s", line.rstrip())

    return_code = proc.wait()
    return return_code, "".join(output_lines)


def collect_cluster_context(cluster_name: str, context_dir: Path) -> None:
    """Collect Kubernetes diagnostics from a kind cluster into context_dir.

    Mirrors the procgen server-status action: gathers pod logs, failed pods,
    events, nodes, CRDs, services, deployments, and helm values for every
    namespace, saving everything under context_dir/<category>/.

    Uses the kubeconfig exported from kind directly to avoid --context flag
    ordering issues with kubectl plugins/multi-resource commands.
    """
    # Export the kubeconfig for this cluster into a temp env var so we never
    # need --context anywhere (avoids "flags cannot be placed before plugin name").
    kubeconfig_result = subprocess.run(
        ["kind", "get", "kubeconfig", "--name", cluster_name],
        capture_output=True, text=True, timeout=30,
    )
    kubeconfig_content = kubeconfig_result.stdout
    kubeconfig_env = {**os.environ, "KUBECONFIG": ""}

    # Write kubeconfig to a temp file so subprocesses can share it
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as kf:
        kf.write(kubeconfig_content)
        kubeconfig_path = kf.name

    kubeconfig_env["KUBECONFIG"] = kubeconfig_path
    base_cmd = ["kubectl"]

    def run(args: List[str], out_file: Optional[Path] = None) -> str:
        cmd = base_cmd + args
        logger.debug("  $ %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=kubeconfig_env)
            output = result.stdout + result.stderr
        except Exception as exc:
            output = str(exc)
        if out_file:
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(output, encoding="utf-8")
        return output

    def run_raw(cmd: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=kubeconfig_env)

    try:
        logger.info("Collecting cluster context: %s -> %s", cluster_name, context_dir)
        context_dir.mkdir(parents=True, exist_ok=True)

        # Overview: pods, services, deployments across all namespaces (separate calls to avoid
        # the "flags before plugin name" error that comma-joined multi-resource gets can trigger)
        run(["get", "po", "-A"], context_dir / "pods.txt")
        run(["get", "svc", "-A"], context_dir / "services-overview.txt")
        run(["get", "deploy", "-A"], context_dir / "deployments-overview.txt")

        # Failed (non-Running) pods
        run(["get", "po", "-A", "--field-selector=status.phase!=Running", "-oyaml"],
            context_dir / "failed-pods.yaml")

        # Events sorted by time
        run(["get", "events", "-A", "--sort-by=.lastTimestamp"],
            context_dir / "events.txt")

        # Nodes
        nodes_dir = context_dir / "nodes"
        run(["get", "nodes", "-oyaml"], nodes_dir / "nodes.yaml")
        run(["describe", "nodes"], nodes_dir / "nodes-describe.log")

        # All custom resources — collect every CRD unconditionally
        crds_dir = context_dir / "crds"
        crd_list_output = run(["get", "crd", "--no-headers"])
        for line in crd_list_output.splitlines():
            parts = line.split()
            crd = parts[0] if parts else ""
            if not crd:
                continue
            run(["get", crd, "-A", "-oyaml"], crds_dir / f"{crd}.yaml")

        # Per-namespace pod logs, services, deployments, helm values
        ns_output = run_raw(
            base_cmd + ["get", "ns", "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
        )
        namespaces = [ns for ns in ns_output.stdout.splitlines() if ns.strip()]

        pods_dir = context_dir / "pods"
        svcs_dir = context_dir / "services"
        deploys_dir = context_dir / "deployments"
        helm_dir = context_dir / "helm-values"

        for ns in namespaces:
            # Pods
            pod_output = run_raw(
                base_cmd + ["-n", ns, "get", "po", "-o",
                            "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
            )
            for po in pod_output.stdout.splitlines():
                po = po.strip()
                if not po:
                    continue
                run(["-n", ns, "describe", "po", po], pods_dir / f"{ns}-{po}-describe.log")
                run(["-n", ns, "get", "po", po, "-oyaml"], pods_dir / f"{ns}-{po}-pod.yaml")
                # Container logs (previous then current)
                containers_out = run_raw(
                    base_cmd + ["-n", ns, "get", "po", po, "-o",
                                "jsonpath={range .spec.containers[*]}{.name}{'\\n'}{end}"],
                )
                for container in containers_out.stdout.splitlines():
                    container = container.strip()
                    if not container:
                        continue
                    prev = run_raw(base_cmd + ["-n", ns, "logs", "-p", "-c", container, po], timeout=60)
                    if prev.returncode == 0 and prev.stdout.strip():
                        log_text = prev.stdout
                    else:
                        curr = run_raw(base_cmd + ["-n", ns, "logs", "-c", container, po], timeout=60)
                        log_text = curr.stdout + curr.stderr
                    log_file = pods_dir / f"{ns}-{po}-{container}-logs.log"
                    log_file.parent.mkdir(parents=True, exist_ok=True)
                    log_file.write_text(log_text, encoding="utf-8")

            # Services
            svc_output = run_raw(
                base_cmd + ["-n", ns, "get", "svc", "-o",
                            "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
            )
            for svc in svc_output.stdout.splitlines():
                svc = svc.strip()
                if not svc:
                    continue
                run(["-n", ns, "describe", "svc", svc], svcs_dir / f"{ns}-{svc}-describe.log")
                run(["-n", ns, "get", "svc", svc, "-oyaml"], svcs_dir / f"{ns}-{svc}-svc.yaml")

            # Deployments
            deploy_output = run_raw(
                base_cmd + ["-n", ns, "get", "deploy", "-o",
                            "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}"],
            )
            for deploy in deploy_output.stdout.splitlines():
                deploy = deploy.strip()
                if not deploy:
                    continue
                run(["-n", ns, "describe", "deploy", deploy], deploys_dir / f"{ns}-{deploy}-describe.log")
                run(["-n", ns, "get", "deploy", deploy, "-oyaml"], deploys_dir / f"{ns}-{deploy}-deploy.yaml")

            # Helm values
            helm_output = subprocess.run(
                ["helm", "list", "-n", ns, "-q"],
                capture_output=True, text=True, timeout=30, env=kubeconfig_env,
            )
            for chart in helm_output.stdout.splitlines():
                chart = chart.strip()
                if not chart:
                    continue
                helm_vals = subprocess.run(
                    ["helm", "get", "values", "-n", ns, chart, "-o", "yaml"],
                    capture_output=True, text=True, timeout=30, env=kubeconfig_env,
                )
                out_file = helm_dir / f"{ns}-{chart}.yaml"
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(helm_vals.stdout + helm_vals.stderr, encoding="utf-8")

        logger.info("Context collection complete: %s", context_dir)
    finally:
        os.unlink(kubeconfig_path)


# Number of times to retry "kind create cluster" when it fails pulling the node
# image from Docker Hub. These are transient registry timeouts/rate-limits, not
# test failures, so a short retry avoids spurious red runs (see nightly run flakes
# where 13 unrelated tests all died on "failed to pull image kindest/node").
CLUSTER_CREATE_ATTEMPTS = 3
CLUSTER_CREATE_RETRY_DELAY_SECONDS = 15

# Substrings that mark a cluster-creation failure as a transient image-pull issue
# rather than a real problem with the test or cluster config. Matching is
# case-insensitive. We deliberately keep this narrow so genuine failures fail fast.
_TRANSIENT_PULL_MARKERS = (
    "failed to pull image",
    "registry-1.docker.io",
    "context deadline exceeded",
    "request canceled while waiting for connection",
    "i/o timeout",
    "tls handshake timeout",
)


def _is_transient_pull_failure(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _TRANSIENT_PULL_MARKERS)


def create_cluster_with_retries(cluster_name: str, repo_root: Path) -> Tuple[int, str]:
    """Create a kind cluster, retrying only on transient Docker Hub image-pull failures.

    The first successful pull seeds the local Docker image cache, so later clusters
    in the same job reuse it. A failed attempt may leave a partial cluster behind,
    so we delete by name before retrying. Returns the last (code, output) pair.
    """
    create_code, create_output = 0, ""
    for attempt in range(1, CLUSTER_CREATE_ATTEMPTS + 1):
        create_code, create_output = run_command(["kind", "create", "cluster", "--name", cluster_name], repo_root)
        if create_code == 0:
            return create_code, create_output
        if attempt == CLUSTER_CREATE_ATTEMPTS or not _is_transient_pull_failure(create_output):
            break
        logger.warning(
            "Transient image-pull failure creating cluster '%s' (attempt %d/%d); retrying in %ds",
            cluster_name, attempt, CLUSTER_CREATE_ATTEMPTS, CLUSTER_CREATE_RETRY_DELAY_SECONDS,
        )
        # Clean up any partial cluster so the retry starts from a clean slate.
        run_command(["kind", "delete", "cluster", "--name", cluster_name], repo_root)
        time.sleep(CLUSTER_CREATE_RETRY_DELAY_SECONDS)
    return create_code, create_output


PORT_FORWARD_RE = re.compile(r"\bkubectl\s+port-forward\b")


def contains_port_forward(script_content: str) -> bool:
    """Report whether the script would actually run `kubectl port-forward`.

    Comment-only lines are ignored. A hidden `{{< doc-test >}}` block often
    documents *why* a test avoids port-forwarding, and naming the command in that
    comment used to reject the test even though nothing ran it. A line whose first
    non-whitespace character is `#` is never executed by bash, so skipping those
    cannot hide a real invocation. Trailing comments are deliberately left in
    scope: telling a real `#` from one inside a quoted string or heredoc needs a
    shell parser, and over-reporting is the safe direction here.
    """
    for line in script_content.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if PORT_FORWARD_RE.search(line):
            return True
    return False


# kind names the control-plane container "<cluster>-control-plane" and that
# becomes the Kubernetes Node name, which must be a single DNS label: 63 chars.
# Over that, kubeadm init fails well before any doc content runs, with
# "[-]poststarthook/rbac/bootstrap-roles failed" and a livez timeout rather than
# anything naming the real problem.
#
# The old flat [:50] truncation had 7 chars of headroom and lost it when a
# scenario declaring more than one `type:` started producing `name::type` test
# names: "per-try-timeout-in-gatewaylistener::functional" lands on exactly 64.
# The budget is derived here instead of hardcoded, and a truncated name keeps a
# hash of the full one so two long scenarios sharing a prefix cannot collide.
KIND_NODE_SUFFIX = "-control-plane"
MAX_DNS_LABEL = 63
MAX_CLUSTER_NAME = MAX_DNS_LABEL - len(KIND_NODE_SUFFIX)


def make_cluster_name(prefix: str, test_slug: str) -> str:
    """A kind cluster name whose control-plane node name stays a valid DNS label."""
    name = f"{prefix}-{test_slug}"
    if len(name) <= MAX_CLUSTER_NAME:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{name[:MAX_CLUSTER_NAME - 9]}-{digest}"


def run_test_case(repo_root: Path, test_case: TestCase, cluster_prefix: str, context_base_dir: Optional[Path] = None, pause: bool = False, keep_cluster: bool = False) -> Dict:
    test_slug = sanitize_name(test_case.name)
    cluster_name = make_cluster_name(cluster_prefix, test_slug)

    # Build a unique context dir slug from the full report key (doc_rel::test_name),
    # e.g. content/docs/kubernetes/main/security/csrf.md::default  ->
    #      content-docs-kubernetes-main-security-csrf--default
    # This avoids collisions when the same test name appears in multiple doc versions.
    doc_rel = test_case.document.relative_to(repo_root).as_posix()
    context_slug = sanitize_name(f"{doc_rel.removesuffix('.md')}--{test_case.name}")

    checks: List[str] = []
    status = "failed"
    error: Optional[str] = None
    collected_context_dir: Optional[Path] = None

    logger.info('\n')
    logger.info("=== Running test: %s (%s) ===", test_case.name, doc_rel)

    script_content = test_case.script_path.read_text(encoding="utf-8")
    if contains_port_forward(script_content):
        logger.warning("SKIPPED (port-forward): %s", doc_rel)
        return {
            "status": "failed",
            "checks": checks,
            "error": "Test shell script contains 'kubectl port-forward', which is not supported in automated tests.",
        }

    create_code, create_output = create_cluster_with_retries(cluster_name, repo_root)
    if create_code != 0:
        # Best-effort context collection even if cluster creation partially failed
        if context_base_dir is not None:
            collected_context_dir = context_base_dir / context_slug
            collected_context_dir.mkdir(parents=True, exist_ok=True)
            (collected_context_dir / "test-execution.log").write_text(create_output, encoding="utf-8")
            try:
                collect_cluster_context(cluster_name, collected_context_dir)
            except Exception as exc:
                logger.warning("Context collection skipped: %s", exc)
        return {
            "status": "failed",
            "checks": checks,
            "error": create_output.strip(),
            "cluster": cluster_name,
            **({"context_dir": str(collected_context_dir.relative_to(repo_root))} if collected_context_dir else {}),
        }

    verbose = logger.isEnabledFor(logging.DEBUG)
    already_running = subprocess.run(["pgrep", "-x", "cloud-provider-kind"], capture_output=True).returncode == 0
    if already_running:
        logger.info("cloud-provider-kind already running, skipping start")
        cloud_provider = None
    else:
        cloud_provider = subprocess.Popen(
            ["cloud-provider-kind", "--gateway-channel", "disabled"],
            cwd=str(repo_root),
            stdout=None,
            stderr=None if verbose else subprocess.DEVNULL,
            text=True,
        )

    try:
        time.sleep(2)
        test_code, output = run_command(["bash", test_case.script_path.as_posix()], repo_root)
        checks = [line.strip() for line in output.splitlines() if line.strip().startswith("✓ ")]
        status = "passed" if test_code == 0 else "failed"
        if test_code != 0:
            error = output.strip()
            # Collect cluster diagnostics before the cluster is deleted
            if context_base_dir is not None:
                collected_context_dir = context_base_dir / context_slug
                collected_context_dir.mkdir(parents=True, exist_ok=True)
                (collected_context_dir / "test-execution.log").write_text(output, encoding="utf-8")
                try:
                    collect_cluster_context(cluster_name, collected_context_dir)
                except Exception as exc:
                    logger.warning("Context collection error: %s", exc)
    finally:
        if keep_cluster:
            # Leave the cluster up for an external capture step (e.g. port-forward + Playwright).
            # cloud-provider-kind is still stopped: the pods are up and port-forward to the proxy
            # deployment works without it, and leaving the daemon running would leak a process.
            if cloud_provider is not None:
                cloud_provider.terminate()
                try:
                    cloud_provider.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    cloud_provider.kill()
            logger.info(
                "--keep-cluster set: cluster '%s' left running (kubeconfig context 'kind-%s'). "
                "Clean up with: kind delete cluster --name %s",
                cluster_name, cluster_name, cluster_name,
            )
        else:
            if pause:
                logger.info("--pause set: cluster '%s' is kept running. Press Ctrl+C to clean up and exit.", cluster_name)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    logger.info("Interrupted — deleting cluster '%s'...", cluster_name)

            if cloud_provider is not None:
                cloud_provider.terminate()
                try:
                    cloud_provider.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    cloud_provider.kill()

            delete_code, delete_output = run_command(["kind", "delete", "cluster", "--name", cluster_name], repo_root)
            if delete_code != 0 and not error:
                error = delete_output.strip()
                status = "failed"

    result = {
        "status": status,
        "checks": checks,
        "cluster": cluster_name,
    }
    if error:
        result["error"] = error
    if collected_context_dir is not None:
        result["context_dir"] = str(collected_context_dir.relative_to(repo_root))
    return result


def write_report(
    report_path: Path,
    tested_documents: List[str],
    test_results: Dict[str, Dict],
    total_documents: int = 0,
    total_by_version: Optional[Dict[str, int]] = None,
    skipped: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    report = {
        "tested_documents": tested_documents,
        "total_documents": total_documents,
        "total_documents_by_version": total_by_version or {},
        "tests": test_results,
    }
    # Kept out of `tests`: everything that reads `tests` counts any status
    # other than passed as a failure, and a skip here is a warning.
    if skipped:
        report["skipped_needs_update"] = skipped
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(yaml.safe_dump(report, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate and run doc tests from page YAML front matter metadata.")
    parser.add_argument("--repo-root", default=".", help="Workspace root")
    parser.add_argument(
        "--docs-tests-root",
        default=None,
        help="Path to a docs-tests checkout, for {{< doc-test file=\"...\" >}} external "
        "content and scenario manifests. Defaults to $DOCS_TESTS_ROOT, else the docs-tests "
        "checkout this script runs from.",
    )
    parser.add_argument(
        "--upstream-root",
        default=None,
        help="Path to a checkout of the repo this site rebases its content from. Only "
        "needed for a site whose pages are {{< rebase >}} shells: without it such a "
        "page extracts to nothing, because the shell is all there is on disk. Can also "
        "be set via the UPSTREAM_ROOT environment variable.",
    )
    parser.add_argument(
        "--upstream-versions",
        default=None,
        help="JSON object mapping this site's versions to the upstream version root "
        "that feeds each, e.g. '{\"latest\": \"latest\"}'. A version left out is "
        "skipped rather than guessed, which is how versions fed by a frozen upstream "
        "tree stay out. Can also be set via UPSTREAM_VERSIONS.",
    )
    parser.add_argument("--docs-glob", default="content/docs/**/*.md", help="Glob to discover markdown docs")
    parser.add_argument("--version", default="2.2.x", help="Default context.version")
    parser.add_argument("--product", default="kubernetes", help="Default context.product")
    parser.add_argument(
        "--generated-dir",
        default="out/tests/generated",
        help="Directory where generated scripts/manifests are written",
    )
    parser.add_argument(
        "--report-file",
        default="out/tests/generated/test-results.yaml",
        help="YAML report file path",
    )
    parser.add_argument("--cluster-prefix", default="doc-test", help="Kind cluster name prefix")
    parser.add_argument(
        "--types",
        default=None,
        metavar="LIST",
        help="Comma-separated test types to run, e.g. 'functional,live'. Default: all types. "
        "'schema' = config-vs-schema comparison, no execution. 'functional' = static "
        "validation against a real cluster, always runs, no vendor dependency (the default "
        "for any scenario that doesn't declare a type). 'live' = reachable-but-unauthenticated "
        "check against a real external endpoint. 'credentialed' = check against a real "
        "vendor; run this on its own non-blocking schedule, never on a doc PR.",
    )
    parser.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream all command output (default: enabled)",
    )
    parser.add_argument("--generate-only", action="store_true", help="Only generate scripts/manifests, do not run tests")
    parser.add_argument("--list-tests", action="store_true", help="Print discovered test cases as JSON to stdout and exit")
    parser.add_argument("--file", nargs="+", default=None, metavar="FILE", help="Path(s) to one or more markdown files to test (relative to repo root or absolute)")
    parser.add_argument("--test", default=None, help="Name of a specific test scenario to run (only used when --file specifies a single file)")
    parser.add_argument("--pause", action="store_true", help="After the test, keep the cluster running until Ctrl+C, then clean up")
    parser.add_argument(
        "--keep-cluster",
        action="store_true",
        help="After the test, leave the kind cluster running and exit (non-blocking) instead of deleting it. "
        "Use for screenshot capture: an external step can port-forward the proxy and run Playwright against "
        "the kept cluster (kubeconfig context 'kind-<cluster>'), then run 'kind delete cluster --name <cluster>'.",
    )
    parser.add_argument(
        "--keep-cluster-file",
        default=None,
        metavar="PATH",
        help="With --keep-cluster, write the kept cluster name(s) to PATH (one per line) so CI can port-forward and later delete them.",
    )
    parser.add_argument(
        "--annotation-report",
        action="append",
        default=[],
        metavar="PATH",
        help="Report from `annotations.py attach --report` (repeatable, one per attached checkout). "
             "A scenario that would select markup which did not attach is skipped with a warning, "
             "not run with steps missing.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    repo_root = Path(args.repo_root).resolve()
    generated_dir = (repo_root / args.generated_dir).resolve()
    report_path = (repo_root / args.report_file).resolve()
    docs_tests_root = resolve_docs_tests_root(args.docs_tests_root)

    upstream_root_value = args.upstream_root or os.environ.get("UPSTREAM_ROOT")
    upstream_root = Path(upstream_root_value).resolve() if upstream_root_value else None
    upstream_versions_value = args.upstream_versions or os.environ.get("UPSTREAM_VERSIONS")
    upstream_version_for: Dict[str, str] = {}
    if upstream_versions_value:
        try:
            upstream_version_for = json.loads(upstream_versions_value)
        except json.JSONDecodeError as exc:
            logger.error("--upstream-versions is not valid JSON: %s", exc)
            return 1
    if upstream_root and not upstream_version_for:
        # Fail rather than expand nothing: a rebase shell with no version map
        # extracts to an empty script that passes, which is the failure mode this
        # whole framework exists to avoid.
        logger.error("--upstream-root was given without --upstream-versions; a rebase "
                     "shell cannot be resolved without knowing which upstream version "
                     "feeds each of this site's versions.")
        return 1

    if args.file:
        filter_test_name = args.test if len(args.file) == 1 else None
        # Naming ONE file is a deliberate act (someone debugging a specific
        # guide), so it is always honoured. A bulk list is CI passing its
        # changed files, and there the frozen trees have to be filtered out to
        # match build_test_cases(); see TESTED_VERSIONS.
        skip_frozen = len(args.file) > 1
        test_cases = []
        tested_docs: List[str] = []
        skipped_frozen: List[str] = []
        # A named file's scenarios can live in a manifest instead of its front
        # matter, and once an area is converted its front matter is stripped.
        # Reading front matter alone here made a PR's changed-file selection log
        # "No test metadata" and drop those tests, and made the per-test run
        # (`--file X --test Y`) fail with "No test named" for a test that
        # full discovery had just listed. Same page-scoped precedence as
        # build_test_cases(): the manifest wins over front matter on one page.
        manifest_cases, claimed, _ = build_test_cases_from_manifests(
            repo_root, docs_tests_root, generated_dir, filter_test_name=filter_test_name
        )
        manifest_cases_by_doc: Dict[str, List[TestCase]] = {}
        for tc in manifest_cases:
            manifest_cases_by_doc.setdefault(tc.document.relative_to(repo_root).as_posix(), []).append(tc)
        for f in args.file:
            md_file = Path(f)
            if not md_file.is_absolute():
                md_file = repo_root / md_file
            # An empty segment means the path is not a versioned doc at all, so
            # leave it to the "no test metadata" warning below rather than
            # silently dropping it as if it were a frozen release tree.
            version_segment = _version_segment(f)
            if skip_frozen and version_segment and version_segment not in TESTED_VERSIONS:
                skipped_frozen.append(f)
                continue
            cases, docs = build_test_cases_from_file(repo_root, md_file, generated_dir, filter_test_name=filter_test_name)
            try:
                rel_f = md_file.resolve().relative_to(repo_root).as_posix()
            except ValueError:
                rel_f = None
            if rel_f is not None:
                cases = [c for c in cases if (rel_f, c.name.split("::")[0]) not in claimed]
                from_manifest = manifest_cases_by_doc.get(rel_f, [])
                cases.extend(from_manifest)
                if from_manifest:
                    docs = sorted(set(docs) | {rel_f})
            tested_docs.extend(docs)
            if not cases:
                if args.test and len(args.file) == 1:
                    logger.error("No test named '%s' found in %s", args.test, f)
                    return 1
                else:
                    logger.warning("No test metadata found in '%s'.", f)
                    continue
            test_cases.extend(cases)
        if skipped_frozen:
            # Say so out loud. A quietly reduced test count reads as "everything
            # was covered" when it was not.
            by_version: Dict[str, int] = {}
            for f in skipped_frozen:
                by_version[_version_key(f)] = by_version.get(_version_key(f), 0) + 1
            logger.info(
                "Skipped %d file(s) in frozen version trees (%s). Only %s are tested.",
                len(skipped_frozen),
                ", ".join(f"{k}: {n}" for k, n in sorted(by_version.items())),
                "/".join(TESTED_VERSIONS),
            )
        _, all_tested_documents, total_by_version, total_documents = build_test_cases(repo_root, args.docs_glob, generated_dir, docs_tests_root=docs_tests_root)
        tested_documents = sorted(set(tested_docs) | set(all_tested_documents))
    else:
        test_cases, tested_documents, total_by_version, total_documents = build_test_cases(repo_root, args.docs_glob, generated_dir, docs_tests_root=docs_tests_root)

    # "schema" (config-vs-schema, no execution) has no cluster-based run path in this
    # script at all -- it has no prereq chain (nothing installs its CRDs) and no
    # assertion block, only the bare CR to validate statically. Running it through
    # run_test_case would just fail with "no matches for kind ..." against an empty
    # cluster. It's handled exclusively by doc_test_schema_check.py; exclude it here
    # unconditionally, not just when --types happens to ask for something else.
    schema_cases = [tc for tc in test_cases if tc.type == "schema"]
    test_cases = [tc for tc in test_cases if tc.type != "schema"]
    for tc in schema_cases:
        logger.debug(
            "Skipping %s::%s (type 'schema' has no cluster-based run path; use doc_test_schema_check.py)",
            tc.document.relative_to(repo_root).as_posix(), tc.name,
        )

    if args.types:
        allowed_types = {t.strip() for t in args.types.split(",") if t.strip()}
        skipped = [tc for tc in test_cases if tc.type not in allowed_types]
        test_cases = [tc for tc in test_cases if tc.type in allowed_types]
        for tc in skipped:
            logger.debug("Skipping %s::%s (type '%s' not in --types %s)", tc.document.relative_to(repo_root).as_posix(), tc.name, tc.type, args.types)

    if args.list_tests:
        entries = [
            {"file": tc.document.relative_to(repo_root).as_posix(), "test": tc.name, "type": tc.type}
            for tc in test_cases
        ]
        print(json.dumps(entries))
        return 0

    if not test_cases:
        logger.info("No docs with test metadata found.")
        write_report(report_path, tested_documents, {}, total_documents, total_by_version)
        return 0

    unattached = load_annotation_reports(args.annotation_report)
    skipped: Dict[str, Dict[str, Any]] = {}
    runnable: List[TestCase] = []
    for test_case in test_cases:
        logger.debug("Generating script for %s::%s", test_case.document.relative_to(repo_root).as_posix(), test_case.name)
        inferred_version = infer_version_from_sources(test_case.sources, args.version)
        definition = {
            "name": sanitize_name(f"{test_case.document.stem}-{test_case.name}"),
            "main_file": test_case.document.relative_to(repo_root).as_posix(),
            "context": {
                "version": inferred_version,
                "product": args.product,
            },
            "options": DEFAULT_OPTIONS,
            "sources": [
                {
                    "file": src["file"],
                    "paths": [src["path"]],
                    **({"assert": src["assert"]} if src.get("assert") else {}),
                }
                for src in test_case.sources
            ],
            "output": {
                "script": test_case.script_path.relative_to(repo_root).as_posix(),
                "manifest": test_case.manifest_path.relative_to(repo_root).as_posix(),
            },
        }
        read = generate_script_and_manifest(
            repo_root, definition, test_case.script_path, test_case.manifest_path,
            docs_tests_root=docs_tests_root, upstream_root=upstream_root,
            upstream_version_for=upstream_version_for,
        )
        missing = unattached_for(test_case, read, unattached)
        if missing:
            key = f"{test_case.document.relative_to(repo_root).as_posix()}::{test_case.name}"
            skipped[key] = {"markup": [
                {"page": m.page.as_posix(), "kind": m.kind, "paths": ",".join(sorted(m.selectors)), "reason": m.reason}
                for m in missing
            ]}
            logger.warning("SKIPPED: %s: %d piece(s) of its test markup did not attach (needs update)", key, len(missing))
            continue
        runnable.append(test_case)
    test_cases = runnable
    write_job_summary(skipped)

    if args.generate_only:
        write_report(report_path, tested_documents, {}, total_documents, total_by_version, skipped)
        logger.info("Generated %d scripts from metadata", len(test_cases))
        logger.info("Wrote report scaffold: %s", report_path.relative_to(repo_root))
        return 0

    context_base_dir = generated_dir / "context"

    logger.info("Running %d test scenario(s)", len(test_cases))
    if args.keep_cluster and len(test_cases) > 1:
        logger.warning("--keep-cluster with %d scenarios will leave multiple clusters running.", len(test_cases))
    test_results: Dict[str, Dict] = {}
    kept_clusters: List[str] = []
    exit_code = 0
    for test_case in test_cases:
        doc_rel = test_case.document.relative_to(repo_root).as_posix()
        key = f"{doc_rel}::{test_case.name}"
        result = run_test_case(repo_root, test_case, args.cluster_prefix, context_base_dir=context_base_dir, pause=args.pause, keep_cluster=args.keep_cluster)
        result["type"] = test_case.type
        status_icon = "PASSED" if result.get("status") == "passed" else "FAILED"
        logger.info("%s: %s (%s)", status_icon, key, test_case.type)
        test_results[key] = result
        if args.keep_cluster and result.get("cluster"):
            kept_clusters.append(result["cluster"])
        if result.get("status") != "passed":
            exit_code = 1

    if args.keep_cluster and args.keep_cluster_file and kept_clusters:
        kept_path = Path(args.keep_cluster_file)
        if not kept_path.is_absolute():
            kept_path = repo_root / kept_path
        kept_path.parent.mkdir(parents=True, exist_ok=True)
        kept_path.write_text("\n".join(kept_clusters) + "\n", encoding="utf-8")
        logger.info("Wrote kept cluster name(s) to %s", kept_path)

    write_report(report_path, tested_documents, test_results, total_documents, total_by_version, skipped)
    logger.info("================= Test Results =================")
    logger.info("Wrote report: %s", report_path.relative_to(repo_root))
    passed_count = sum(1 for r in test_results.values() if r['status'] == 'passed')
    failed_count = sum(1 for r in test_results.values() if r['status'] != 'passed')
    logger.info("Test results: %d total, %d passed, %d failed, %d skipped (needs update)",
                len(test_cases) + len(skipped), passed_count, failed_count, len(skipped))
    if failed_count > 0:
        logger.info("Failed test results:")
        logger.debug("%s", yaml.safe_dump(test_results, sort_keys=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
