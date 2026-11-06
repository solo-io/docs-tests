#!/usr/bin/env python3
"""Helm values for the 'schema' doc test: check that the values a page sets on
a product chart are values the chart has (solo-io/docs-tests#19).

The product charts ship no values.schema.json, so Helm installs a misspelled or
removed value without complaint. This builds a schema per chart from the chart
itself, at the release the page documents:

- every key in the chart's default `values.yaml`;
- every key its templates read (`.Values.a.b`, `index .Values "a" "b"`), which
  covers options that `values.yaml` only shows commented out;
- a map whose default is empty, a list, a scalar, or that a template hands on
  whole (`toYaml .Values.x`, `range .Values.x`, `with .Values.x` + `toYaml .`,
  `omit .Values.x "k" | toJson`) is open: its contents are not checked, because they are Kubernetes fields or
  free-form labels, not chart options.

A value also has to be the type of its default: a boolean, an integer, a
number, a string, a list, or a map. A default of `null` or `""` says nothing
about the type, so that value takes anything. `--set` values are typed the way
Helm types them (`true` is a boolean, `3` an integer, anything else a string),
and a shell variable or a `<placeholder>` is not checked.

What the generated schema can't know goes in an overlay file in docs-tests
(`products/<product>/helm-values.yaml`): more paths to open, per chart. An open
path takes any keys and any type.

A key that is in `values.yaml` but that no template reads still passes; this
can't tell.
"""

import json
import re
import shlex
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    yaml = None

# Open: contents not checked. Otherwise {"properties": {key: node}}, closed,
# and either form can carry a "type" (TYPE_NAMES) the value must have.
OPEN: Dict[str, Any] = {}
TYPE_NAMES = {"boolean": "a boolean", "integer": "an integer", "number": "a number",
              "string": "a string", "array": "a list", "object": "a map"}

_VALUES_REF = r"\$?\.Values((?:\.[A-Za-z0-9_]+)+)"
VALUES_REF_RE = re.compile(_VALUES_REF)
INDEX_REF_RE = re.compile(r"index\s+\$?\.Values((?:\s+\"[^\"]+\")+)")
# A value handed on whole, so its contents are Kubernetes fields or a
# component's own config, not chart options. `omit`/`pick`/`merge` take a map
# apart or together on its way to toJson, such as enterprise agentgateway's
# `omit .Values.tokenExchange "configSecret" | toJson`.
PASSED_ON_RE = re.compile(r"(?:toYaml|toJson|tpl|omit|pick|merge|mergeOverwrite|deepCopy"
                          r"|range\s+(?:\$\w+\s*,\s*)?(?:\$\w+\s*:=\s*)?)\s*\(?\s*" + _VALUES_REF)
PIPED_ON_RE = re.compile(_VALUES_REF + r"\s*\|\s*(?:toYaml|toJson)")
WITH_RE = re.compile(r"with\s+\(?\s*" + _VALUES_REF)
WITH_BODY_LINES = 4


@dataclass
class HelmChart:
    name: str
    schema: Dict[str, Any]
    source: str                           # for messages: the chart dir
    stale_overlay: List[str] = field(default_factory=list)


def _type_of(default: Any) -> Optional[str]:
    """The type a default says its value has, or None if it says nothing."""
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, int):
        return "integer"
    if isinstance(default, float):
        return "number"
    if isinstance(default, str):
        return "string" if default else None
    if isinstance(default, list):
        return "array"
    if isinstance(default, dict):
        return "object"
    return None


def _schema_for(default: Any) -> Dict[str, Any]:
    t = _type_of(default)
    if isinstance(default, dict) and default:
        return {"type": t, "properties": {str(k): _schema_for(v) for k, v in default.items()}}
    return {"type": t} if t else OPEN


def _template_refs(text: str) -> Tuple[List[List[str]], List[List[str]]]:
    """(paths read, paths handed on whole) from one template file."""
    read: List[List[str]] = []
    passed_on: List[List[str]] = []
    for m in VALUES_REF_RE.finditer(text):
        read.append(m.group(1).lstrip(".").split("."))
    for m in INDEX_REF_RE.finditer(text):
        read.append(re.findall(r"\"([^\"]+)\"", m.group(1)))
    for rx in (PASSED_ON_RE, PIPED_ON_RE):
        for m in rx.finditer(text):
            passed_on.append(m.group(1).lstrip(".").split("."))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in WITH_RE.finditer(line):
            body = "\n".join(lines[i:i + WITH_BODY_LINES + 1])
            if re.search(r"(?:toYaml|toJson|tpl)\s+\.\s", body + " "):
                passed_on.append(m.group(1).lstrip(".").split("."))
    return read, passed_on


def _add_path(schema: Dict[str, Any], path: List[str], open_leaf: bool, keep_type: bool = True) -> None:
    """Add `path` as a key. With `open_leaf`, its contents are not checked; a
    template that hands a value on whole still needs it to be the type of its
    default, so the type stays unless `keep_type` is False."""
    node = schema
    for i, part in enumerate(path):
        props = node.get("properties")
        if props is None:
            return          # already open from here down
        if part not in props:
            props[part] = OPEN
        if i == len(path) - 1 and open_leaf:
            t = props[part].get("type") if keep_type else None
            props[part] = {"type": t} if t else OPEN
        node = props[part]


def _is_closed_key(schema: Dict[str, Any], path: List[str]) -> bool:
    """Whether `path` is a key the schema has and still checks the keys or type
    of, so opening it in the overlay changes something."""
    node = schema
    for part in path:
        props = node.get("properties")
        if props is None or part not in props:
            return False
        node = props[part]
    return "properties" in node or "type" in node


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Every key either schema has. Open only if both are."""
    if "properties" not in a:
        return b
    if "properties" not in b:
        return a
    props = dict(a["properties"])
    for k, v in b["properties"].items():
        props[k] = _merge(props[k], v) if k in props else v
    t = a.get("type") or b.get("type")
    return {"type": t, "properties": props} if t else {"properties": props}


def _subchart_dir(chart_dir: Path, name: str) -> Optional[Path]:
    """A dependency vendored under charts/, as a directory or a packaged .tgz."""
    charts = chart_dir / "charts"
    if (charts / name / "Chart.yaml").exists():
        return charts / name
    for tgz in sorted(charts.glob(f"{name}-*.tgz")):
        out = Path(tempfile.mkdtemp(prefix="helm-subchart-"))
        with tarfile.open(tgz) as tar:
            tar.extractall(out, filter="data")
        if (out / name / "Chart.yaml").exists():
            return out / name
    return None


def _schema_with_subcharts(chart_dir: Path, meta: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    """A dependency's values go under its alias (or name). A subchart that isn't
    vendored, as in a chart directory from source, can't be checked: its key is open.
    `global` and `tags` are Helm's own and shared with every subchart."""
    props = schema["properties"]
    for dep in meta.get("dependencies") or []:
        key = str(dep.get("alias") or dep.get("name"))
        sub_dir = _subchart_dir(chart_dir, str(dep.get("name")))
        sub = load_chart(sub_dir).schema if sub_dir else OPEN
        props[key] = _merge(props.get(key, {"properties": {}}), sub) if sub_dir else OPEN
    if meta.get("dependencies"):
        props["global"] = OPEN
        props["tags"] = OPEN
    return schema


def load_chart(chart_dir: Path, overlay: Optional[Dict[str, Any]] = None) -> HelmChart:
    """The chart's name (Chart.yaml) and a schema of its values."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    meta = yaml.safe_load((chart_dir / "Chart.yaml").read_text(encoding="utf-8")) or {}
    name = str(meta.get("name") or chart_dir.name)
    values_file = chart_dir / "values.yaml"
    defaults = yaml.safe_load(values_file.read_text(encoding="utf-8")) if values_file.exists() else None
    # The top level is always closed: a chart with no values takes none.
    schema: Dict[str, Any] = {"properties": dict(_schema_for(defaults).get("properties", {}))}
    # A subchart can read `.Values.global`, so every chart takes it.
    schema["properties"].setdefault("global", OPEN)
    passed_on: List[List[str]] = []
    for tpl in sorted((chart_dir / "templates").rglob("*")) if (chart_dir / "templates").is_dir() else []:
        if not tpl.is_file():
            continue
        read, whole = _template_refs(tpl.read_text(encoding="utf-8", errors="replace"))
        for path in read:
            _add_path(schema, path, open_leaf=False)
        passed_on.extend(whole)
    for path in passed_on:
        _add_path(schema, path, open_leaf=True)
    _schema_with_subcharts(chart_dir, meta, schema)

    stale: List[str] = []
    for dotted in ((overlay or {}).get(name) or {}).get("open") or []:
        path = split_key(str(dotted))
        if not _is_closed_key(schema, path):
            stale.append(str(dotted))
        _add_path(schema, path, open_leaf=True, keep_type=False)
    return HelmChart(name=name, schema=schema, source=chart_dir.as_posix(), stale_overlay=stale)


def load_overlay(path: Path) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def split_key(key: str) -> List[str]:
    """`a.b\\.c[0].d` -> ["a", "b.c", "d"]: Helm's --set path, list indexes dropped
    (a list is open, so nothing below one is checked)."""
    parts: List[str] = []
    cur = ""
    i = 0
    while i < len(key):
        ch = key[i]
        if ch == "\\" and i + 1 < len(key):
            cur += key[i + 1]
            i += 2
            continue
        if ch == ".":
            parts.append(cur)
            cur = ""
        elif ch == "[":
            parts.append(cur)
            cur = ""
            i = key.find("]", i)
            if i < 0:
                break
            parts.append("[]")
            if i + 1 < len(key) and key[i + 1] == ".":
                i += 1
        else:
            cur += ch
        i += 1
    if cur:
        parts.append(cur)
    out: List[str] = []
    for p in parts:
        if p == "[]":
            break           # into a list: open
        if p:
            out.append(p)
    return out


def unknown_keys(schema: Dict[str, Any], values: Any, prefix: Tuple[str, ...] = ()) -> List[str]:
    """Dotted paths in `values` that the schema has no key for."""
    props = schema.get("properties")
    if props is None or not isinstance(values, dict):
        return []
    found: List[str] = []
    for k, v in values.items():
        path = prefix + (str(k),)
        if str(k) not in props:
            found.append(".".join(path))
        else:
            found.extend(unknown_keys(props[str(k)], v, path))
    return found


# A value whose type can't be known from the page: a list item set by index
# (`a[0]=x`), a shell variable, or a placeholder.
ANY = object()
UNCHECKED_RE = re.compile(r"\$|<[^<>\s][^<>]*>|\{\{")


def _matches(t: str, value: Any) -> bool:
    if value is None:
        return True         # null unsets the value
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def wrong_types(schema: Dict[str, Any], values: Any, prefix: Tuple[str, ...] = ()) -> List[Tuple[str, str, Any]]:
    """(dotted path, expected type, value) for each value in `values` that is
    not the type the schema says. Nothing below a wrong value is checked."""
    if values is ANY or isinstance(values, str) and UNCHECKED_RE.search(values):
        return []
    t = schema.get("type")
    if t and not _matches(t, values):
        return [(".".join(prefix), t, values)]
    props = schema.get("properties")
    if props is None or not isinstance(values, dict):
        return []
    found: List[Tuple[str, str, Any]] = []
    for k, v in values.items():
        if str(k) in props:
            found.extend(wrong_types(props[str(k)], v, prefix + (str(k),)))
    return found


def _describe(value: Any) -> str:
    kind = ("a boolean" if isinstance(value, bool) else "an integer" if isinstance(value, int)
            else "a number" if isinstance(value, float) else "a string" if isinstance(value, str)
            else "a list" if isinstance(value, list) else "a map" if isinstance(value, dict) else "a value")
    if isinstance(value, (dict, list)):
        return kind
    shown = json.dumps(value)
    return f"{kind}, {shown if len(shown) <= 40 else shown[:37] + '...'}"


def _typed_set(raw: str) -> Any:
    """A `--set` value the way Helm's strvals parser types it: `true`/`false` a
    boolean, `null` nothing, a whole number without a leading zero an integer,
    `{a,b}` a list, and anything else a string (so `0.5` is a string)."""
    if raw.startswith("{") and raw.endswith("}"):
        return [_typed_set(p) for p in _split_sets(raw[1:-1])]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    if raw == "0":
        return 0
    if raw and raw[0] != "0" and re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    return raw


def set_value(flag: str, key: str, raw: str) -> Any:
    """The value a `--set`-style flag gives `key`, typed as Helm types it."""
    if UNCHECKED_RE.search(raw):
        value: Any = ANY
    elif flag == "--set-json":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = ANY
    elif flag == "--set":
        value = _typed_set(raw)
    else:
        value = raw         # --set-string, --set-literal, --set-file: always a string
    if "[" in key:
        value = [ANY]       # split_key stops at the list
    return value


def _nest(path: List[str], value: Any) -> Dict[str, Any]:
    out: Any = value
    for part in reversed(path):
        out = {part: out}
    return out


# --- Reading Helm commands from a page -------------------------------------

HELM_SUBCOMMANDS = {"install", "upgrade", "template"}
SET_FLAGS = {"--set", "--set-string", "--set-json", "--set-file", "--set-literal"}
VALUES_FLAGS = {"-f", "--values"}
# Flags that take a value, so the word after them is not the chart.
VALUE_FLAGS = SET_FLAGS | VALUES_FLAGS | {
    "-n", "--namespace", "--version", "--repo", "--timeout", "--kube-context", "--kubeconfig",
    "-o", "--output", "--output-dir", "--post-renderer", "--post-renderer-args", "--description",
    "--name-template", "--username", "--password", "--ca-file", "--cert-file", "--key-file",
    "--history-max", "-a", "--api-versions", "--kube-version", "-l", "--labels", "--keyring",
    "--registry-config", "--repository-config", "--repository-cache", "--max-history",
    "--kube-apiserver", "--kube-as-user", "--kube-as-group", "--kube-token", "-s", "--show-only",
    "--release-name", "--wait-for",
}
HEREDOC_START_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
WRITE_RE = re.compile(r"^\s*(?:cat\b[^\n]*?>>?\s*|tee\s+(?:-a\s+)?)([^\s<>|;&]+)")


@dataclass
class HelmCommand:
    chart_ref: str
    line: int                               # offset in the block, for ordering
    sets: List[Tuple[str, str]] = field(default_factory=list)   # (flag, "key=value")
    value_files: List[str] = field(default_factory=list)
    stdin_values: Optional[str] = None
    third_party_repo: bool = False


def _heredocs(text: str) -> List[Tuple[int, int, str, str]]:
    """(start offset, end offset, opening line, body) of each heredoc."""
    out = []
    for m in HEREDOC_START_RE.finditer(text):
        tag = m.group(1)
        line_end = text.find("\n", m.end())
        if line_end < 0:
            continue
        end = re.compile(r"^\s*" + re.escape(tag) + r"\s*$", re.MULTILINE).search(text, line_end + 1)
        if not end:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        out.append((line_start, end.end(), text[line_start:line_end], text[line_end + 1:end.start()]))
    return out


def _split_sets(value: str) -> List[str]:
    """`a=1,b={x,y}` -> ["a=1", "b={x,y}"]: Helm's comma list, braces kept whole."""
    parts, cur, depth = [], "", 0
    for ch in value:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p for p in parts if p.strip()]


def parse_block(text: str) -> Tuple[List[HelmCommand], List[Tuple[int, str, str]]]:
    """The Helm install/upgrade/template commands in one shell block, and the
    files it writes with a heredoc: (offset, file name, YAML)."""
    docs = _heredocs(text)
    writes: List[Tuple[int, str, str]] = []
    commands: List[HelmCommand] = []
    # Blank out heredoc bodies so their lines are not read as commands.
    masked = list(text)
    for start, end, opening, body in docs:
        w = WRITE_RE.search(opening)
        if w and "helm" not in opening:
            writes.append((start, w.group(1).strip("'\""), body))
        body_start = start + len(opening) + 1
        for i in range(body_start, end):
            if masked[i] != "\n":
                masked[i] = " "
    flat = "".join(masked).replace("\\\n", "  ")
    for m in re.finditer(r"(?:^|[;&|(]\s*|\s)helm\s+(install|upgrade|template)\b([^\n;|&]*)", flat, re.MULTILINE):
        args = m.group(2)
        try:
            words = shlex.split(args, comments=False, posix=True)
        except ValueError:
            words = args.split()
        cmd = HelmCommand(chart_ref="", line=m.start())
        positionals: List[str] = []
        i = 0
        while i < len(words):
            w = words[i]
            flag, eq, inline = w.partition("=") if w.startswith("--") else (w, "", "")
            if flag in VALUE_FLAGS:
                if eq:
                    val = inline
                else:
                    i += 1
                    val = words[i] if i < len(words) else ""
                if flag in SET_FLAGS:
                    cmd.sets.extend((flag, s) for s in _split_sets(val))
                elif flag in VALUES_FLAGS:
                    if val == "-":
                        # The command's own heredoc: the first one that ends after it starts.
                        heredoc = next((d for d in docs if d[1] > m.start()), None)
                        cmd.stdin_values = heredoc[3] if heredoc else None
                    else:
                        cmd.value_files.append(val)
                elif flag == "--repo":
                    cmd.third_party_repo = True
            elif w.startswith("<<") or w.startswith(">") or w in {"<", "EOF"}:
                break
            elif not w.startswith("-"):
                positionals.append(w)
            i += 1
        if positionals:
            cmd.chart_ref = positionals[-1]
            commands.append(cmd)
    return commands, writes


def chart_name(ref: str) -> str:
    """`oci://cr.agentgateway.dev/charts/agentgateway` -> `agentgateway`."""
    return ref.rstrip("/").rsplit("/", 1)[-1].split(":", 1)[0]


@dataclass
class HelmFinding:
    loc: str
    message: str


def check_blocks(blocks: Iterable[Any], charts: Dict[str, HelmChart], loc_for) -> Tuple[int, List[HelmFinding]]:
    """Check the Helm values that `blocks` (one page's, in any order) set on a
    known chart. Returns (values checked, findings).

    A values file a command reads with `-f NAME` is matched to the heredocs on
    the same page that write NAME before that command and after the previous
    command that read NAME (both tabs of a "with X / without X" choice count).
    A file written on another page is not checked.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    events: List[Tuple[Tuple[str, int, int], Any, Any]] = []   # ((file, line, offset), kind, payload)
    for b in blocks:
        cmds, writes = parse_block(b.content)
        base = (b.file_path.as_posix(), b.start_line)
        for off, name, body in writes:
            events.append(((base[0], base[1], off), "write", (name, body, b)))
        for c in cmds:
            events.append(((base[0], base[1], c.line), "cmd", (c, b)))
    events.sort(key=lambda e: e[0])

    checked = 0
    findings: List[HelmFinding] = []
    pending: Dict[Tuple[str, str], List[Tuple[str, Any]]] = {}    # (file, name) -> writes
    for (file, _, _), kind, payload in events:
        if kind == "write":
            name, body, b = payload
            pending.setdefault((file, name), []).append((body, b))
            continue
        cmd, b = payload
        chart = None if cmd.third_party_repo else charts.get(chart_name(cmd.chart_ref))
        sources: List[Tuple[Any, str, Any]] = []        # (values, what, block)
        for flag, item in cmd.sets:
            key, eq, raw = item.partition("=")
            if not eq:
                continue
            value = set_value(flag, key.strip(), raw)
            sources.append((_nest(split_key(key.strip()), value), f"{flag} {key.strip()}", b))
        if cmd.stdin_values is not None:
            sources.append((cmd.stdin_values, "values from stdin", b))
        for name in cmd.value_files:
            for body, wb in pending.pop((file, name), []):
                sources.append((body, f"values file {name}", wb))
        if chart is None:
            continue
        for values, what, src_block in sources:
            if isinstance(values, str):
                try:
                    values = yaml.safe_load(values)
                except yaml.YAMLError:
                    continue
            if not isinstance(values, dict):
                continue
            checked += 1
            for key in unknown_keys(chart.schema, values):
                findings.append(HelmFinding(loc_for(src_block),
                                            f"chart {chart.name} has no value '{key}' ({what})"))
            for key, t, value in wrong_types(chart.schema, values):
                findings.append(HelmFinding(loc_for(src_block),
                                            f"chart {chart.name} value '{key}' must be {TYPE_NAMES[t]}, "
                                            f"not {_describe(value)} ({what})"))
    return checked, findings
