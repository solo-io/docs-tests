#!/usr/bin/env python3

import argparse
import json
import os
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import yaml as _yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    _yaml = None


SHELL_LANGS = {"", "sh", "bash", "shell", "zsh", "yaml", "yml"}


@dataclass
class CodeBlock:
    file_path: Path
    start_line: int
    language: str
    paths: List[str]
    content: str
    hidden: bool = False
    # True for a block synthesized from a front-matter step's `assert:` list
    # rather than parsed from an inline {{< doc-test >}} shortcode in the page
    # body. start_line is borrowed from its anchor block in this case (see
    # _synthesize_assert_blocks) -- callers use this flag to label it correctly.
    from_metadata: bool = False
    # None for every block extracted directly from text (real blocks never
    # share a start_line, so ordering among them is unambiguous). Set to
    # group_index*1000+offset for a synthesized block, to order it immediately
    # after its anchor -- which shares the same start_line -- and relative to
    # sibling entries from the same assert: list. See select_blocks().
    synthetic_order: Optional[int] = None


@dataclass
class TestInclude:
    file_path: Path
    start_line: int
    paths: List[str]
    test_file: Path


@dataclass
class FileResult:
    file_path: Path
    expanded_text: str
    code_blocks: List[CodeBlock] = field(default_factory=list)
    test_includes: List[TestInclude] = field(default_factory=list)
    links: List[str] = field(default_factory=list)


def _load_link_version_map(repo_root: Path, product: Optional[str] = None) -> Dict[str, str]:
    """Build a {linkVersion: version} mapping from the Hugo config.

    Hugo's version shortcode resolves URL tokens like "latest" or "main" to
    their canonical version strings (e.g. "1.5.x", "1.4.x") via this mapping.
    The Python extractor must perform the same lookup so that include-if
    comparisons work correctly.

    Two config schemas are supported:

    * docs-theme-extras v1.0.0 and later, where a single ``params.versions``
      list holds every version and each entry tags the ``sections`` it applies
      to. When ``product`` names a section, entries tagged for that section win
      over entries that are not, so two sections with different version lists
      each resolve correctly.
    * The earlier per-section schema, where each ``params.sections.<name>``
      carried its own ``versions`` list.

    Both are read because a consumer repo can be mid-migration, and different
    consumer repos are on different extras versions at any given time.
    The current schema takes precedence when a linkVersion appears in both.

    Returns an empty dict if the config is missing or cannot be parsed.
    """
    if _yaml is None:
        return {}
    hugo_yaml = repo_root / "hugo.yaml"
    if not hugo_yaml.exists():
        hugo_yaml = repo_root / "config.yaml"
    if not hugo_yaml.exists():
        return {}
    try:
        data = _yaml.safe_load(hugo_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    params = data.get("params") or {}
    mapping: Dict[str, str] = {}

    # Current schema: one flat params.versions list tagged by section. Entries
    # for this product are applied first so they win over other sections'.
    versions = [e for e in (params.get("versions") or []) if isinstance(e, dict)]
    tagged = [e for e in versions if product and product in (e.get("sections") or [])]
    # setdefault keeps the first writer, so the tagged pass wins and repeating
    # the full list afterwards only fills in linkVersions it did not cover.
    for entry in tagged + versions:
        link_ver = entry.get("linkVersion")
        ver = entry.get("version")
        if link_ver and ver:
            mapping.setdefault(link_ver, ver)

    # Legacy schema: params.sections.<name>.versions[].
    sections = params.get("sections") or {}
    for section_data in sections.values():
        if not isinstance(section_data, dict):
            continue
        for entry in section_data.get("versions") or []:
            if not isinstance(entry, dict):
                continue
            link_ver = entry.get("linkVersion")
            ver = entry.get("version")
            if link_ver and ver:
                mapping.setdefault(link_ver, ver)
    return mapping


# This file lives in docs-tests/scripts/, so the checkout it runs from IS the
# docs-tests root. Consumers no longer keep their own copies of these scripts.
THIS_DOCS_TESTS_ROOT = Path(__file__).resolve().parent.parent


def resolve_docs_tests_root(value: Optional[str] = None) -> Path:
    """The docs-tests checkout: an explicit path, else $DOCS_TESTS_ROOT, else this one.

    Never None. A missing root used to mean "no manifests": every scenario that
    lives only in docs-tests vanished from discovery without an error, and a PR
    run went green having skipped them.
    """
    value = value or os.environ.get("DOCS_TESTS_ROOT")
    return Path(value).resolve() if value else THIS_DOCS_TESTS_ROOT


class Extractor:
    def __init__(
        self,
        repo_root: Path,
        definition: dict,
        docs_tests_root: Optional[Path] = None,
        upstream_root: Optional[Path] = None,
        upstream_version_for: Optional[Dict[str, str]] = None,
        asset_roots: Optional[List[Path]] = None,
    ):
        self.repo_root = repo_root
        # Where `{{< reuse >}}` targets are looked up, in order. Normally just this
        # repo. When expanding content rebased from upstream it is [consumer,
        # upstream], because the consuming site's own assets SHADOW the upstream
        # copies it unions in -- that is how a site overrides a shared snippet.
        # Resolving upstream-first would silently pick the generic copy: the real
        # case that prompted this is an install snippet whose consumer copy adds
        # the enterprise license steps and whose upstream copy has none, so the
        # test would have installed the wrong build and passed.
        self.asset_roots: List[Path] = [r.resolve() for r in (asset_roots or [repo_root])]
        # A consuming site may REBASE a whole upstream page rather than reuse a
        # snippet: the page's entire body is `{{< rebase file="..." >}}`, and the
        # upstream content is unioned in when the site builds. Without these two,
        # such a page extracts to zero code blocks -- the shell is all there is on
        # disk. `upstream_root` is a checkout of the repo the content comes from;
        # `upstream_version_for` maps each of this site's versions to the upstream
        # version root that feeds it. Both unset means rebase is left unexpanded,
        # which is correct for any site that does not use it.
        self.upstream_root = upstream_root.resolve() if upstream_root else None
        self.upstream_version_for = dict(upstream_version_for or {})
        self._upstream_extractors: Dict[str, "Extractor"] = {}
        # Upstream pages inlined by {{< rebase >}}. Kept apart from
        # recursion_edges, whose paths the manifest makes relative to repo_root.
        self.rebased_files: Set[Path] = set()
        # Root of a `docs-tests` checkout, used to resolve `{{< doc-test file="..." >}}`
        # external content; see resolve_docs_tests_root. Independent of repo_root:
        # content read from here is never relativized against repo_root.
        self.docs_tests_root = Path(docs_tests_root).resolve() if docs_tests_root else resolve_docs_tests_root()
        self.definition = definition
        options = definition.get("options", {})
        self.follow_reuse = bool(options.get("follow_reuse", True))
        self.follow_include = bool(options.get("follow_include", True))
        self.follow_internal_links = bool(options.get("follow_internal_links", True))
        self.max_depth = int(options.get("max_depth", 8))
        self.skip_tabs_without_paths = bool(options.get("skip_tabs_without_paths", True))
        self.version = definition.get("context", {}).get("version")
        self.product = definition.get("context", {}).get("product")

        # Map linkVersion tokens (e.g. "latest", "main") to canonical version
        # strings (e.g. "1.5.x", "1.4.x") as defined in hugo.yaml.  This
        # mirrors what Hugo's version.html shortcode does at build time so that
        # include-if comparisons resolve correctly.
        self._link_version_map = _load_link_version_map(repo_root, self.product)
        # Resolve the context version token to its canonical version string once.
        self._resolved_version = self._link_version_map.get(self.version, self.version) if self.version else self.version

        self.sources = definition.get("sources", [])
        self.main_file = self._resolve_workspace_path(definition["main_file"])

        self.path_selectors_by_file: Dict[Path, Set[str]] = {}
        # A step's `assert:` list (docs-tests-relative paths, in order) names the
        # hidden assertion content directly in front matter, instead of requiring
        # an inline {{< doc-test paths="X" file="Y" >}} shortcode in the page body.
        # Keyed by source file, value is a list of (path_selector, [assert files])
        # in declaration order -- a file can have more than one scenario's step
        # (e.g. two named scenarios both stepping through the same page).
        self.external_asserts_by_file: Dict[Path, List[Tuple[str, List[str]]]] = {}
        for source in self.sources:
            source_file = self._resolve_workspace_path(source["file"])
            selectors = set(source.get("paths", []))
            if source_file in self.path_selectors_by_file:
                self.path_selectors_by_file[source_file].update(selectors)
            else:
                self.path_selectors_by_file[source_file] = selectors

            assert_files = source.get("assert")
            if assert_files:
                for selector in source.get("paths", []):
                    self.external_asserts_by_file.setdefault(source_file, []).append(
                        (selector, list(assert_files))
                    )

        self.file_cache: Dict[Path, FileResult] = {}
        self.visited: Set[Path] = set()
        self.recursion_edges: List[Tuple[str, str, str]] = []

        # Memoization caches for link resolution. _route_for_file and
        # _find_by_route_like_target are pure with respect to the (static)
        # all_markdown_files set, but get called once per link per visited
        # file. Without caching, walk() is ~O(links x files^2) of filesystem
        # stat calls, which is pathologically slow for pages that link into
        # densely cross-linked hub pages.
        self._route_cache: Dict[Path, Optional[str]] = {}
        self._target_cache: Dict[str, Optional[Path]] = {}

        self.all_markdown_files = [
            p
            for p in self.repo_root.rglob("*.md")
            if "/public/" not in p.as_posix() and "/resources/" not in p.as_posix()
        ]

    def _resolve_workspace_path(self, path_value: str) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path.resolve()
        return (self.repo_root / path).resolve()

    def _read_file(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _parse_shortcode_params(self, params: str) -> Dict[str, str]:
        return {m.group(1): m.group(2) for m in re.finditer(r'([\w-]+)="([^"]*)"', params)}

    def _evaluate_version_block(self, params: str) -> bool:
        if not self.version:
            return True
        kv = self._parse_shortcode_params(params)
        include_if = [x.strip() for x in kv.get("include-if", "").split(",") if x.strip()]
        exclude_if = [x.strip() for x in kv.get("exclude-if", "").split(",") if x.strip()]
        # Identifiers that represent the current version. We accept both the
        # canonical version string (e.g. "2.2.x") AND the stable linkVersion
        # token (e.g. "main"/"latest"). This lets include-if/exclude-if target a
        # directory by its release-stable name ("main") instead of the version
        # number, which rotates every release. Matching the canonical string
        # still works regardless of whether context.version was supplied as a
        # linkVersion token or directly as a version string. Mirrors the
        # linkVersion matching in the Hugo version.html shortcode.
        identifiers = {self.version, self._resolved_version}
        for link_ver, ver in self._link_version_map.items():
            if ver == self._resolved_version:
                identifiers.add(link_ver)
        identifiers.discard(None)
        if include_if and not identifiers.intersection(include_if):
            return False
        if exclude_if and identifiers.intersection(exclude_if):
            return False
        return True

    def _strip_version_blocks(self, text: str) -> str:
        pattern = re.compile(r"\{\{[<%]\s*version\s*([^%>]*)[ \t]*[>%]\}\}(.*?)\{\{[<%]\s*/version\s*[>%]\}\}", re.DOTALL)

        def repl(match: re.Match) -> str:
            params = match.group(1) or ""
            body = match.group(2) or ""
            return body if self._evaluate_version_block(params) else ""

        return pattern.sub(repl, text)

    def _resolve_reuse(self, source_file: Path, asset_path: str, depth: int) -> str:
        if depth > self.max_depth:
            return ""
        trimmed = asset_path.lstrip("/")
        candidate = None
        for root in self.asset_roots:
            attempt = (root / "assets" / trimmed).resolve()
            if attempt.exists():
                candidate = attempt
                break
        if candidate is None:
            return ""
        self.recursion_edges.append((source_file.as_posix(), candidate.as_posix(), "reuse"))
        return self._expand_text(candidate, self._read_file(candidate), depth + 1).rstrip("\n")

    def _resolve_include(self, source_file: Path, include_path: str, depth: int) -> str:
        if depth > self.max_depth:
            return ""
        rel = include_path.strip().strip("\"")
        rel = rel.strip("/")
        candidates = []
        include_candidate = self.repo_root / "content" / rel
        if include_candidate.suffix == ".md":
            candidates.append(include_candidate)
        else:
            candidates.extend([include_candidate.with_suffix(".md"), include_candidate / "_index.md"])

        for candidate in candidates:
            if candidate.exists():
                resolved = candidate.resolve()
                self.recursion_edges.append((source_file.as_posix(), resolved.as_posix(), "include"))
                return self._expand_text(resolved, self._read_file(resolved), depth + 1)
        return ""

    def _expand_text(self, source_file: Path, text: str, depth: int = 0) -> str:
        if depth > self.max_depth:
            return text

        text = self._strip_version_blocks(text)

        def replace_reuse(match: re.Match) -> str:
            shortcode = match.group(1)
            asset_path = match.group(2)
            if shortcode not in {"reuse", "reuse-append"} or not self.follow_reuse:
                return match.group(0)
            return self._resolve_reuse(source_file, asset_path, depth)

        text = re.sub(r"\{\{[<%]\s*(reuse|reuse-append)\s+\"([^\"]+)\"\s*[>%]\}\}", replace_reuse, text)

        def replace_rebase(match: re.Match) -> str:
            return self._resolve_rebase(source_file, match.group(1), depth)

        text = re.sub(r"\{\{[<%]\s*rebase\s+file=\"([^\"]+)\"[^}]*[>%]\}\}", replace_rebase, text)

        def replace_include(match: re.Match) -> str:
            include_path = match.group(1)
            if not self.follow_include:
                return match.group(0)
            return self._resolve_include(source_file, include_path, depth)

        text = re.sub(r"\{\{[%<]\s*include\s+\"([^\"]+)\"\s*[%>]\}\}", replace_include, text)

        def replace_link_hextra(match: re.Match) -> str:
            return match.group(1)

        text = re.sub(r"\{\{<\s*link-hextra\s+path=\"([^\"]+)\"\s*>\}\}", replace_link_hextra, text)

        text = self._resolve_conditional_text_blocks(text)

        return text


    def _resolve_rebase(self, source_file: Path, file_value: str, depth: int) -> str:
        """Inline an upstream page in place of a `{{< rebase >}}` shortcode.

        The `file=` value names a mode and a path below it but carries NO version:
        the version comes from the consuming page's own path, which is how one
        shell serves several of the site's versions. It also points at an
        assembled asset directory that is gitignored and only exists after a
        build, so the path is inverted to the upstream repo's source rather than
        looked up.

        The inlined text is expanded against the UPSTREAM root, because any
        `{{< reuse >}}` inside it names upstream assets that do not exist here.
        The resulting blocks stay attributed to the consuming page, which is what
        a scenario's `path:` selectors are declared against, and matches what the
        site itself renders.
        """
        if self.upstream_root is None or not self.upstream_version_for:
            return ""
        parts = file_value.strip("/").split("/")
        if len(parts) < 3:
            return ""
        mode, rest = parts[1], "/".join(parts[2:])

        rel = source_file.resolve().relative_to(self.repo_root.resolve()).as_posix()
        version = next((seg for seg in rel.split("/") if seg in self.upstream_version_for), None)
        if version is None:
            # A version deliberately absent from the map (its upstream root is a
            # frozen release tree). Expanding it would imply coverage that cannot
            # exist, so leave the page empty rather than silently borrow another
            # version's content.
            return ""

        upstream_rel = f"content/docs/{mode}/{self.upstream_version_for[version]}/{rest}"
        upstream_file = self.upstream_root / upstream_rel
        if not upstream_file.is_file():
            raise FileNotFoundError(
                f"rebase target not found: {source_file} references {file_value!r}, "
                f"which resolves to {upstream_rel} in the upstream checkout at "
                f"{self.upstream_root}. The page exists but its source does not, so "
                f"it would silently test nothing."
            )

        self.rebased_files.add(upstream_file.resolve())
        text = upstream_file.read_text(encoding="utf-8")
        body = text.split("---", 2)[2] if text.startswith("---") and text.count("---") >= 2 else text

        key = f"{mode}/{self.upstream_version_for[version]}"
        extractor = self._upstream_extractors.get(key)
        if extractor is None:
            extractor = Extractor(
                repo_root=self.upstream_root,
                definition={
                    "main_file": upstream_rel,
                    "sources": [],
                    "options": self.definition.get("options", {}),
                    "context": {"product": mode, "version": self.upstream_version_for[version]},
                },
                docs_tests_root=self.docs_tests_root,
                asset_roots=[self.repo_root, self.upstream_root],
            )
            self._upstream_extractors[key] = extractor
        return extractor._expand_text(upstream_file, body, depth + 1)

    def _resolve_conditional_text_blocks(self, text: str) -> str:
        pattern = re.compile(
            r"\{\{[<%]\s*conditional-text\s*([^%>]*)[ \t]*[>%]\}\}(.*?)\{\{[<%]\s*/conditional-text\s*[>%]\}\}",
            re.DOTALL,
        )

        def repl(match: re.Match) -> str:
            params = self._parse_shortcode_params(match.group(1) or "")
            body = match.group(2) or ""
            include_if = [x.strip() for x in params.get("include-if", "").split(",") if x.strip()]
            exclude_if = [x.strip() for x in params.get("exclude-if", "").split(",") if x.strip()]

            if include_if:
                if not self.product:
                    return ""
                if self.product not in include_if:
                    return ""

            if exclude_if and self.product and self.product in exclude_if:
                return ""

            return body

        return pattern.sub(repl, text)

    def _extract_links(self, text: str) -> List[str]:
        found = []
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
            target = match.group(1).strip()
            if not target:
                continue
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if target:
                found.append(target)
        return found

    def _find_by_route_like_target(self, target: str) -> Optional[Path]:
        if target in self._target_cache:
            return self._target_cache[target]
        result = self._find_by_route_like_target_uncached(target)
        self._target_cache[target] = result
        return result

    def _find_by_route_like_target_uncached(self, target: str) -> Optional[Path]:
        stripped = target.strip().strip("/")
        if not stripped:
            return None

        direct_candidates = [
            self.repo_root / "content" / stripped,
            self.repo_root / "assets" / stripped,
            self.repo_root / "content" / f"{stripped}.md",
            self.repo_root / "assets" / f"{stripped}.md",
            self.repo_root / "content" / stripped / "_index.md",
            self.repo_root / "assets" / stripped / "_index.md",
            self.repo_root / "content" / "docs" / stripped / "_index.md",
            self.repo_root / "content" / "docs" / f"{stripped}.md",
            self.repo_root / "assets" / "agw-docs" / "pages" / f"{stripped}.md",
            self.repo_root / "assets" / "agw-docs" / "pages" / stripped / "_index.md",
        ]
        for candidate in direct_candidates:
            if candidate.is_file():
                return candidate.resolve()

        last_segment = stripped.split("/")[-1]
        route_suffix = f"/{stripped}/"

        best: Optional[Path] = None
        best_score: Optional[int] = None
        for file_path in self.all_markdown_files:
            route_guess = self._route_for_file(file_path)
            if route_guess and route_guess.endswith(route_suffix):
                score = len(file_path.as_posix())
            elif file_path.stem == last_segment or (file_path.name == "_index.md" and file_path.parent.name == last_segment):
                score = len(file_path.as_posix()) + 1000
            else:
                continue

            if best is None or score < best_score:
                best = file_path.resolve()
                best_score = score

        return best

    def _route_for_file(self, file_path: Path) -> Optional[str]:
        if file_path in self._route_cache:
            return self._route_cache[file_path]
        route = self._route_for_file_uncached(file_path)
        self._route_cache[file_path] = route
        return route

    def _route_for_file_uncached(self, file_path: Path) -> Optional[str]:
        p = file_path.resolve()
        if (self.repo_root / "content") in p.parents:
            rel = p.relative_to(self.repo_root / "content").as_posix()
            if rel.endswith("/_index.md"):
                rel = rel[: -len("/_index.md")]
            elif rel.endswith(".md"):
                rel = rel[: -len(".md")]
            return "/" + rel.strip("/") + "/"
        if (self.repo_root / "assets" / "agw-docs" / "pages") in p.parents:
            rel = p.relative_to(self.repo_root / "assets" / "agw-docs" / "pages").as_posix()
            if rel.endswith("/_index.md"):
                rel = rel[: -len("/_index.md")]
            elif rel.endswith(".md"):
                rel = rel[: -len(".md")]
            return "/" + rel.strip("/") + "/"
        return None

    def _extract_code_blocks(self, source_file: Path, text: str) -> List[CodeBlock]:
        blocks: List[CodeBlock] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            open_match = re.match(r"^\s*(`{3,})(.*)$", line)
            if not open_match:
                i += 1
                continue

            fence = open_match.group(1)
            info = (open_match.group(2) or "").strip()
            start_line = i + 1
            lang = ""
            if info:
                lang = re.split(r"[,{\s]", info, maxsplit=1)[0].strip().lower()

            j = i + 1
            content_lines = []
            close_pattern = re.compile(rf"^\s*`{{{len(fence)},}}\s*$")
            while j < len(lines) and not close_pattern.match(lines[j]):
                content_lines.append(lines[j])
                j += 1

            attrs = info
            path_match = re.search(r'paths\s*=\s*"([^"]+)"', attrs)
            paths = []
            if path_match:
                paths = [x.strip() for x in path_match.group(1).split(",") if x.strip()]

            content = "\n".join(content_lines)
            content = textwrap.dedent(content).rstrip() + "\n"

            blocks.append(
                CodeBlock(
                    file_path=source_file,
                    start_line=start_line,
                    language=lang,
                    paths=paths,
                    content=content,
                )
            )

            i = j + 1 if j < len(lines) else j
        return blocks

    def _extract_hidden_shell_blocks(self, source_file: Path, text: str) -> List[CodeBlock]:
        blocks: List[CodeBlock] = []
        # Format: {{< doc-test paths="..." >}}\nBODY\n{{< /doc-test >}}
        # An empty shortcode template (layouts/shortcodes/doc-test.html) causes
        # Hugo to emit nothing for these blocks, keeping tests out of HTML output.
        pattern = re.compile(r"\{\{<\s*doc-test\b([^>]*?)>\}\}(.*?)\{\{<\s*/doc-test\s*>\}\}", re.DOTALL)

        for match in pattern.finditer(text):
            attrs = match.group(1) or ""
            body = match.group(2) or ""
            params = self._parse_shortcode_params(attrs)
            paths = [x.strip() for x in params.get("paths", "").split(",") if x.strip()]
            start_line = text[: match.start()].count("\n") + 1

            external_file = params.get("file")
            if external_file:
                # Content lives in docs-tests, not inline. The shortcode body is
                # expected to be empty in this form: {{< doc-test paths="..."
                # file="..." >}}{{< /doc-test >}}. Resolved against docs_tests_root,
                # never repo_root, so this never touches the repo_root-relative
                # manifest/script logic elsewhere in this file.
                resolved = (self.docs_tests_root / external_file).resolve()
                if not resolved.exists():
                    raise FileNotFoundError(
                        f"doc-test file not found: {external_file} "
                        f"(resolved to {resolved}, from {source_file}:{start_line}; "
                        f"docs_tests_root={self.docs_tests_root})"
                    )
                content = resolved.read_text(encoding="utf-8").strip("\n")
            else:
                content = textwrap.dedent(body).strip("\n")
            if not content:
                continue

            blocks.append(
                CodeBlock(
                    file_path=source_file,
                    start_line=start_line,
                    language="sh",
                    paths=paths,
                    content=content + "\n",
                    hidden=True,
                )
            )

        return blocks

    def _synthesize_assert_blocks(self, source_file: Path, existing_blocks: List[CodeBlock]) -> List[CodeBlock]:
        """Build hidden CodeBlocks for a step's front-matter `assert:` list.

        Anchored to the FIRST existing block (visible or inline-hidden) in this
        file carrying the same path selector, and given that block's exact
        start_line -- not a sentinel. A page's own paths="X" selector is often
        reused by more than one block (e.g. the apply step and, much later, a
        Cleanup section's `kubectl delete`); sorting a synthesized assertion to
        the very end of the file would run it after that later block instead of
        right after the step it actually asserts on. select_blocks()'s sort_key
        uses synthetic_order to break the resulting tie in declaration order,
        after the real anchor block.
        """
        blocks: List[CodeBlock] = []
        entries = self.external_asserts_by_file.get(source_file.resolve(), [])
        for group_index, (path_selector, assert_files) in enumerate(entries):
            anchor_line = next(
                (b.start_line for b in existing_blocks if path_selector in b.paths),
                None,
            )
            if anchor_line is None:
                # No block anywhere in this file carries this selector -- nothing to
                # anchor to. Shouldn't happen for a correctly-declared scenario,
                # since paths= is exactly what ties an assert: list to its config
                # block; fall back to sorting after everything rather than crashing.
                anchor_line = max((b.start_line for b in existing_blocks), default=0) + 10**6
            for offset, rel_file in enumerate(assert_files):
                resolved = (self.docs_tests_root / rel_file).resolve()
                if not resolved.exists():
                    raise FileNotFoundError(
                        f"doc-test assert file not found: {rel_file} (resolved to {resolved}, "
                        f"declared in front matter for {source_file} path={path_selector}; "
                        f"docs_tests_root={self.docs_tests_root})"
                    )
                content = resolved.read_text(encoding="utf-8").strip("\n")
                if not content:
                    continue
                blocks.append(
                    CodeBlock(
                        file_path=source_file,
                        start_line=anchor_line,
                        language="sh",
                        paths=[path_selector],
                        content=content + "\n",
                        hidden=True,
                        from_metadata=True,
                        synthetic_order=group_index * 1000 + offset,
                    )
                )
        return blocks

    def _extract_test_includes(self, source_file: Path, text: str) -> List[TestInclude]:
        includes: List[TestInclude] = []
        pattern = re.compile(r"<!--\s*doc-test-include\b([^>]*)-->")

        for match in pattern.finditer(text):
            attrs = match.group(1) or ""
            params = self._parse_shortcode_params(attrs)
            test_file_value = params.get("file")
            if not test_file_value:
                continue

            include_path = Path(test_file_value)
            test_file = include_path if include_path.is_absolute() else (source_file.parent / include_path)
            test_file = test_file.resolve()

            paths = [x.strip() for x in params.get("paths", "").split(",") if x.strip()]
            start_line = text[: match.start()].count("\n") + 1

            includes.append(
                TestInclude(
                    file_path=source_file,
                    start_line=start_line,
                    paths=paths,
                    test_file=test_file,
                )
            )

        return includes

    def process_file(self, file_path: Path, depth: int = 0) -> FileResult:
        file_path = file_path.resolve()
        if file_path in self.file_cache:
            return self.file_cache[file_path]

        raw = self._read_file(file_path)
        expanded = self._expand_text(file_path, raw, depth)
        code_blocks = self._extract_code_blocks(file_path, expanded)
        code_blocks.extend(self._extract_hidden_shell_blocks(file_path, expanded))
        code_blocks.extend(self._synthesize_assert_blocks(file_path, code_blocks))
        test_includes = self._extract_test_includes(file_path, expanded)
        links = self._extract_links(expanded)

        result = FileResult(
            file_path=file_path,
            expanded_text=expanded,
            code_blocks=code_blocks,
            test_includes=test_includes,
            links=links,
        )
        self.file_cache[file_path] = result
        return result

    def walk(self) -> None:
        queue: List[Tuple[Path, int]] = [(self.main_file, 0)]
        for source_file in self.path_selectors_by_file:
            queue.append((source_file, 0))

        while queue:
            file_path, depth = queue.pop(0)
            file_path = file_path.resolve()
            if file_path in self.visited:
                continue
            if depth > self.max_depth:
                continue
            self.visited.add(file_path)

            result = self.process_file(file_path, depth)
            if not self.follow_internal_links:
                continue

            for link in result.links:
                linked = self._find_by_route_like_target(link)
                if linked and linked.is_file() and linked not in self.visited:
                    self.recursion_edges.append((file_path.as_posix(), linked.as_posix(), "link"))
                    queue.append((linked, depth + 1))

    def read_files(self) -> Set[Path]:
        """Every file this scenario's blocks could come from, in either repo.

        Walked pages, reused snippets, includes, and rebased upstream pages with
        whatever they reuse. Used to skip a scenario whose markup did not attach.
        """
        files = set(self.visited) | set(self.rebased_files)
        files |= {Path(dst) for _, dst, kind in self.recursion_edges if kind != "link"}
        for extractor in self._upstream_extractors.values():
            files |= extractor.read_files()
        return {p.resolve() for p in files}

    def select_blocks(self) -> List[CodeBlock]:
        selected: List[CodeBlock] = []
        # Preserve source order (helm -> gateway -> sample-app -> feature) so that
        # e.g. the Gateway is created before the HTTPRoute that references it.
        source_order = {p.resolve(): i for i, p in enumerate(self.path_selectors_by_file.keys())}
        for source_file, selectors in self.path_selectors_by_file.items():
            result = self.file_cache.get(source_file.resolve())
            if not result:
                continue
            for block in result.code_blocks:
                if self.skip_tabs_without_paths and not block.paths:
                    continue
                if block.language not in SHELL_LANGS:
                    continue
                if selectors and ("all" in selectors or "all" in block.paths or set(block.paths).intersection(selectors)):
                    selected.append(block)
        # Emit blocks in source order (prereqs first), then by line within each file,
        # so hidden blocks (e.g. start server in background) appear before dependent blocks.
        # A synthesized assert block (see _synthesize_assert_blocks) shares its anchor
        # block's start_line by design -- real blocks never collide on start_line, so
        # the synthetic_order tiebreak (-1 for every real block) only ever activates
        # for that one case, sorting the synthetic block right after its anchor and
        # before any later block, in assert: list order.
        def sort_key(b: CodeBlock) -> Tuple[int, int, int]:
            idx = source_order.get(b.file_path.resolve(), 999)
            tie = -1 if b.synthetic_order is None else b.synthetic_order
            return (idx, b.start_line, tie)

        selected.sort(key=sort_key)
        #selected.sort(key=lambda b: (b.file_path, b.start_line))
        return selected

    def select_test_includes(self) -> List[TestInclude]:
        selected: List[TestInclude] = []
        for source_file, selectors in self.path_selectors_by_file.items():
            result = self.file_cache.get(source_file.resolve())
            if not result:
                continue
            for test_include in result.test_includes:
                if self.skip_tabs_without_paths and not test_include.paths:
                    continue
                if selectors and ("all" in selectors or "all" in test_include.paths or set(test_include.paths).intersection(selectors)):
                    selected.append(test_include)
        return selected

    def build_script(self, blocks: List[CodeBlock], test_includes: List[TestInclude]) -> str:
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        seen = set()
        for block in blocks:
            content = block.content.strip("\n")
            if not content:
                continue
            if content in seen:
                continue
            seen.add(content)
            rel = block.file_path.relative_to(self.repo_root).as_posix()
            if block.hidden and block.from_metadata:
                lines.append(f"# Hidden source (front-matter assert, anchored at {rel}:{block.start_line}): paths={','.join(block.paths)}")
            elif block.hidden:
                lines.append(f"# Hidden source: {rel}:{block.start_line} paths={','.join(block.paths)}")
            else:
                lines.append(f"# Source: {rel}:{block.start_line} paths={','.join(block.paths)}")
            lines.append(content)
            lines.append("")

        seen_tests = set()
        for test_include in test_includes:
            rel_source = test_include.file_path.relative_to(self.repo_root).as_posix()
            rel_test = test_include.test_file.relative_to(self.repo_root).as_posix()
            if rel_test in seen_tests:
                continue
            seen_tests.add(rel_test)
            if not test_include.test_file.exists():
                raise FileNotFoundError(
                    f"doc-test-include file not found: {rel_test} (from {rel_source}:{test_include.start_line})"
                )
            lines.append(
                f"# Test include: {rel_source}:{test_include.start_line} paths={','.join(test_include.paths)} file={rel_test}"
            )
            lines.append(f"bun test {rel_test}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def build_manifest(self, blocks: List[CodeBlock], test_includes: List[TestInclude]) -> dict:
        discovered_paths_by_file = {}
        for path, result in self.file_cache.items():
            paths = sorted({p for b in result.code_blocks for p in b.paths} | {p for t in result.test_includes for p in t.paths})
            discovered_paths_by_file[path.relative_to(self.repo_root).as_posix()] = paths

        return {
            "name": self.definition.get("name"),
            "main_file": self.main_file.relative_to(self.repo_root).as_posix(),
            "sources": self.sources,
            "options": self.definition.get("options", {}),
            "context": self.definition.get("context", {}),
            "visited_files": sorted([p.relative_to(self.repo_root).as_posix() for p in self.visited]),
            "recursion_edges": [
                {
                    "from": Path(src).relative_to(self.repo_root).as_posix() if Path(src).is_absolute() else src,
                    "to": Path(dst).relative_to(self.repo_root).as_posix() if Path(dst).is_absolute() else dst,
                    "kind": kind,
                }
                for src, dst, kind in self.recursion_edges
            ],
            "discovered_paths_by_file": discovered_paths_by_file,
            "selected_blocks": [
                {
                    "file": b.file_path.relative_to(self.repo_root).as_posix(),
                    "line": b.start_line,
                    "language": b.language,
                    "paths": b.paths,
                    "hidden": b.hidden,
                    "from_metadata": b.from_metadata,
                    "preview": b.content.splitlines()[0] if b.content.splitlines() else "",
                }
                for b in blocks
            ],
            "selected_test_includes": [
                {
                    "file": t.file_path.relative_to(self.repo_root).as_posix(),
                    "line": t.start_line,
                    "paths": t.paths,
                    "test_file": t.test_file.relative_to(self.repo_root).as_posix(),
                }
                for t in test_includes
            ],
            "selected_count": len(blocks),
            "selected_test_count": len(test_includes),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate executable doc test scripts from markdown path selectors.")
    parser.add_argument("--definition", required=True, help="Path to JSON test definition file")
    parser.add_argument("--repo-root", default=".", help="Workspace root")
    parser.add_argument(
        "--docs-tests-root",
        default=None,
        help="Path to a docs-tests checkout, for {{< doc-test file=\"...\" >}} external "
        "content. Defaults to $DOCS_TESTS_ROOT, else the docs-tests checkout this script runs from.",
    )
    parser.add_argument("--output-script", help="Override output script path")
    parser.add_argument("--output-manifest", help="Override output manifest path")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    definition_path = (repo_root / args.definition).resolve() if not Path(args.definition).is_absolute() else Path(args.definition)
    docs_tests_root = resolve_docs_tests_root(args.docs_tests_root)

    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    extractor = Extractor(repo_root=repo_root, definition=definition, docs_tests_root=docs_tests_root)
    extractor.walk()

    blocks = extractor.select_blocks()
    test_includes = extractor.select_test_includes()
    script = extractor.build_script(blocks, test_includes)
    manifest = extractor.build_manifest(blocks, test_includes)

    output_script = args.output_script or definition.get("output", {}).get("script")
    output_manifest = args.output_manifest or definition.get("output", {}).get("manifest")

    if not output_script or not output_manifest:
        raise ValueError("Definition must provide output.script and output.manifest, or pass CLI overrides")

    script_path = (repo_root / output_script).resolve()
    manifest_path = (repo_root / output_manifest).resolve()
    script_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    script_path.write_text(script, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote script: {script_path.relative_to(repo_root)}")
    print(f"Wrote manifest: {manifest_path.relative_to(repo_root)}")
    print(f"Selected blocks: {len(blocks)}")
    print(f"Selected tests: {len(test_includes)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
