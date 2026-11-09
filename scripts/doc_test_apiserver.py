#!/usr/bin/env python3
"""CRD CEL rules for the 'schema' doc test: send each custom resource a page
shows to a real Kubernetes API server as a dry run (solo-io/docs-tests#19).

`jsonschema` ignores `x-kubernetes-validations`, and Kubernetes CEL has its own
libraries (quantities, URLs, IPs and CIDRs, `.?` optional fields), so only an
API server evaluates them correctly. This runs a bare one, as controller-runtime's
envtest does: `etcd` plus `kube-apiserver`, no nodes and no controllers. Each
version tree gets its own server with its own CRDs installed, and each manifest
is created with `dryRun=All`, so nothing is stored and manifests that share a
name don't collide.

What this adds over the static check: CEL rules, and the API server's own
checks (object names, format and enum checks the schema check can't express).
What it can't know: whether the resource works. There are no controllers, so a
Gateway is never programmed and a policy never attaches.

The binaries come from the envtest releases controller-tools publishes (the
same ones `setup-envtest` downloads), checked against the release index's
sha512, and are cached under ~/.cache/docs-tests/envtest. Pass --envtest-dir,
or set KUBEBUILDER_ASSETS, to use binaries already on disk.

A failure on a value that is a shell variable (`${HOST}`) is not reported,
the same as in the static check: the shell fills it in before the resource
reaches the API server. A rule on an object that holds a shell variable
anywhere below it is not reported either, since it may compare that value.
"""

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    yaml = None

logger = logging.getLogger(__name__)

ENVTEST_INDEX = "https://raw.githubusercontent.com/kubernetes-sigs/controller-tools/HEAD/envtest-releases.yaml"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "docs-tests" / "envtest"
BINARIES = ("etcd", "kube-apiserver")
SHELL_VAR_RE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*")
TOKEN = "docs-tests"


# --- Binaries ---------------------------------------------------------------

def _platform() -> str:
    system = platform.system().lower()
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{system}-{machine}"


def _parse_version(v: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def pick_release(releases: Dict[str, Any], wanted: Optional[str]) -> str:
    """The envtest release to use for Kubernetes `wanted` (e.g. "1.36" or
    "1.36.0"): the newest patch of that minor, else the newest older minor.
    Without `wanted`, the newest release."""
    versions = sorted(releases, key=_parse_version)
    if not versions:
        raise RuntimeError("the envtest release index lists no releases")
    if not wanted:
        return versions[-1]
    minor = _parse_version(wanted)[:2]
    same = [v for v in versions if _parse_version(v)[:2] == minor]
    if same:
        return same[-1]
    older = [v for v in versions if _parse_version(v)[:2] < minor]
    if not older:
        raise RuntimeError(f"no envtest release for Kubernetes {wanted} or older")
    logger.warning("No envtest release for Kubernetes %s; using %s", wanted, older[-1])
    return older[-1]


def _curl(url: str, out: Optional[Path] = None) -> bytes:
    """Download with curl, which uses the system's CA certificates (a
    python.org Python on macOS has none of its own)."""
    cmd = ["curl", "-sSfL", "--retry", "3", url] + (["-o", str(out)] if out else [])
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f"could not download {url}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def ensure_binaries(kubernetes_version: Optional[str], envtest_dir: Optional[str] = None) -> Path:
    """A directory holding `etcd` and `kube-apiserver`: `envtest_dir`, else
    KUBEBUILDER_ASSETS, else the envtest release for `kubernetes_version`,
    downloaded once into the cache with curl."""
    given = envtest_dir or os.environ.get("KUBEBUILDER_ASSETS")
    if given:
        d = Path(given)
        missing = [b for b in BINARIES if not (d / b).is_file()]
        if missing:
            raise RuntimeError(f"{d} has no {', '.join(missing)}")
        return d
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    releases = (yaml.safe_load(_curl(ENVTEST_INDEX)) or {}).get("releases") or {}
    release = pick_release(releases, kubernetes_version)
    d = CACHE_DIR / f"{release}-{_platform()}"
    if all((d / b).is_file() for b in BINARIES):
        return d
    name = f"envtest-{release}-{_platform()}.tar.gz"
    entry = releases[release].get(name)
    if not entry:
        raise RuntimeError(f"envtest {release} has no build for {_platform()}")
    logger.info("Downloading envtest %s (%s)", release, _platform())
    d.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=d.parent) as tmp:
        archive = Path(tmp) / name
        _curl(entry["selfLink"], archive)
        digest = hashlib.sha512(archive.read_bytes()).hexdigest()
        if digest != entry["hash"]:
            raise RuntimeError(f"{name}: sha512 {digest} does not match the release index")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        staged = Path(tmp) / "controller-tools" / "envtest"
        if d.exists():
            shutil.rmtree(d)
        shutil.move(str(staged), str(d))
    return d


# --- CRDs -------------------------------------------------------------------

def load_crds(paths: List[Path]) -> Dict[Tuple[str, str], dict]:
    """(group, kind) -> CRD manifest, from each file or directory of *.yaml in
    `paths`. A later path wins for the same kind, as in the schema check."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    crds: Dict[Tuple[str, str], dict] = {}
    for path in paths:
        files = [path] if path.is_file() else sorted(path.glob("*.yaml"))
        for f in files:
            try:
                docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
            except yaml.YAMLError:
                continue        # the schema check already warned
            for doc in docs:
                if isinstance(doc, dict) and doc.get("kind") == "CustomResourceDefinition":
                    spec = doc.get("spec") or {}
                    kind = (spec.get("names") or {}).get("kind")
                    if kind:
                        crds[(spec.get("group", ""), kind)] = doc
    return crds


@dataclass
class Resource:
    plural: str
    namespaced: bool


def _mapping(crds: Dict[Tuple[str, str], dict]) -> Dict[Tuple[str, str], Resource]:
    return {k: Resource(plural=d["spec"]["names"]["plural"], namespaced=d["spec"].get("scope") != "Cluster")
            for k, d in crds.items()}


# --- Reading the API server's answer -----------------------------------------

def field_path(field_ref: str) -> List[Any]:
    """`spec.rules[0].matches[1].path` -> ["spec", "rules", 0, "matches", 1, "path"].
    Map keys in brackets (`labels[app]`) stay strings."""
    parts: List[Any] = []
    for m in re.finditer(r"([^.\[\]]+)|\[([^\]]*)\]", field_ref or ""):
        name, index = m.group(1), m.group(2)
        if name is not None:
            parts.append(name)
        else:
            parts.append(int(index) if index.isdigit() else index.strip("\"'"))
    return parts


def _at(doc: Any, path: List[Any]) -> Any:
    for part in path:
        try:
            doc = doc[part]
        except (KeyError, IndexError, TypeError):
            return None
    return doc


def _has_shell_var(node: Any) -> bool:
    if isinstance(node, str):
        return bool(SHELL_VAR_RE.search(node))
    if isinstance(node, dict):
        return any(_has_shell_var(k) or _has_shell_var(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_has_shell_var(v) for v in node)
    return False


def rejection(manifest: dict, status: dict) -> Optional[str]:
    """What to report for the API server's Status, or None if every cause is
    about a shell variable. A cause names the field it is about; one with no
    field is about the whole object."""
    causes = (status.get("details") or {}).get("causes") or []
    if not causes:
        return status.get("message") or json.dumps(status)
    kept = []
    for c in causes:
        f = c.get("field") or ""
        node = _at(manifest, field_path(f)) if f else manifest
        if _has_shell_var(node):
            continue
        kept.append(f"at {f or '(root)'}: {c.get('message') or c.get('reason')}")
    return "; ".join(kept) or None


# --- The server ---------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE    # the server's own self-signed cert, on 127.0.0.1
# No proxy: the server is local, and on macOS urllib's system proxy lookup
# hangs in a forked worker.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=_SSL))


def _request(url: str, method: str = "GET", body: Optional[dict] = None, timeout: float = 30) -> Tuple[int, dict]:
    data = json.dumps(body, default=str).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw, code = exc.read(), exc.code
    try:
        return code, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return code, {"message": raw.decode(errors="replace").strip()}


@dataclass
class ApiServerClient:
    """What a worker process needs to send dry runs: picklable, and inherited
    across the pool's fork."""
    url: str
    resources: Dict[Tuple[str, str], Resource] = field(default_factory=dict)

    def dry_run(self, manifest: dict) -> Optional[str]:
        """None if the API server accepts `manifest`, else why not."""
        api_version = str(manifest.get("apiVersion", ""))
        group, _, version = api_version.rpartition("/")
        res = self.resources.get((group, str(manifest.get("kind"))))
        if res is None:
            return None         # not a kind this server has; the static check covers it
        base = f"{self.url}/apis/{group}/{version}"
        # The body sent, not the page's: a name or namespace the page leaves
        # out or takes from a shell variable is filled in, so only the
        # resource's own rules decide. The static check takes a fragment
        # without a name, too.
        manifest = json.loads(json.dumps(manifest, default=str))
        meta = manifest.get("metadata")
        if not isinstance(meta, dict):
            meta = manifest["metadata"] = {}
        if not meta.get("name") and not meta.get("generateName"):
            meta["generateName"] = "docs-tests-"
        if res.namespaced:
            ns = meta.get("namespace") or "default"
            if _has_shell_var(ns):
                ns = meta["namespace"] = "default"
            base += f"/namespaces/{ns}"
        url = f"{base}/{res.plural}?dryRun=All&fieldValidation=Strict"
        # A CRD can take a moment to be served after it is Established.
        for attempt in range(20):
            code, status = _request(url, "POST", manifest)
            if code != 404 or attempt == 19:
                break
            time.sleep(0.25)
        if 200 <= code < 300:
            return None
        why = rejection(manifest, status)
        return f"rejected by the Kubernetes API server: {why}" if why else None


class ApiServer:
    """etcd and kube-apiserver for one version tree, with its CRDs installed."""

    def __init__(self, binaries: Path, crds: Dict[Tuple[str, str], dict], label: str):
        self.binaries = binaries
        self.crds = crds
        self.label = label
        self.tmp = Path(tempfile.mkdtemp(prefix="docs-tests-apiserver-"))
        self.procs: List[subprocess.Popen] = []
        self.client: Optional[ApiServerClient] = None

    def start(self, timeout: float = 120) -> ApiServerClient:
        etcd_port, peer_port, api_port = _free_port(), _free_port(), _free_port()
        etcd_url = f"http://127.0.0.1:{etcd_port}"
        log = (self.tmp / "etcd.log").open("w")
        self.procs.append(subprocess.Popen([
            str(self.binaries / "etcd"), "--data-dir", str(self.tmp / "etcd"),
            "--listen-client-urls", etcd_url, "--advertise-client-urls", etcd_url,
            "--listen-peer-urls", f"http://127.0.0.1:{peer_port}", "--unsafe-no-fsync",
        ], stdout=log, stderr=subprocess.STDOUT))

        key, pub = self.tmp / "sa.key", self.tmp / "sa.pub"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
        tokens = self.tmp / "tokens.csv"
        tokens.write_text(f'{TOKEN},docs-tests,docs-tests,"system:masters"\n')
        log = (self.tmp / "kube-apiserver.log").open("w")
        self.procs.append(subprocess.Popen([
            str(self.binaries / "kube-apiserver"),
            "--etcd-servers", etcd_url, "--cert-dir", str(self.tmp / "certs"),
            "--bind-address", "127.0.0.1", "--secure-port", str(api_port),
            "--service-cluster-ip-range", "10.0.0.0/24", "--allow-privileged=true",
            "--authorization-mode", "AlwaysAllow", "--token-auth-file", str(tokens),
            "--service-account-issuer", "https://docs-tests.local",
            "--service-account-key-file", str(pub), "--service-account-signing-key-file", str(key),
            # A page's namespace doesn't exist here, and nothing runs pods.
            "--disable-admission-plugins", "NamespaceLifecycle,ServiceAccount",
            "--enable-priority-and-fairness=false",
        ], stdout=log, stderr=subprocess.STDOUT))

        url = f"https://127.0.0.1:{api_port}"
        deadline = time.monotonic() + timeout
        while True:
            for p in self.procs:
                if p.poll() is not None:
                    raise RuntimeError(f"{Path(p.args[0]).name} exited ({p.returncode}); see {self.tmp}")
            try:
                if _request(f"{url}/readyz", timeout=5)[0] == 200:
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"kube-apiserver was not ready after {timeout:.0f}s; see {self.tmp}")
            time.sleep(0.5)

        self._install_crds(url, deadline)
        self.client = ApiServerClient(url=url, resources=_mapping(self.crds))
        logger.info("API server for %s: %d CRD(s) installed", self.label, len(self.crds))
        return self.client

    def _install_crds(self, url: str, deadline: float) -> None:
        crd_api = f"{url}/apis/apiextensions.k8s.io/v1/customresourcedefinitions"
        names = []
        for (group, kind), doc in self.crds.items():
            body = {k: v for k, v in doc.items() if k != "status"}
            code, status = _request(crd_api, "POST", body)
            if not 200 <= code < 300:
                raise RuntimeError(f"could not install the {kind} ({group}) CRD: {status.get('message')}")
            names.append(doc["metadata"]["name"])
        pending = set(names)
        while pending:
            for name in list(pending):
                _, crd = _request(f"{crd_api}/{name}")
                conds = (crd.get("status") or {}).get("conditions") or []
                if any(c.get("type") == "Established" and c.get("status") == "True" for c in conds):
                    pending.discard(name)
            if pending and time.monotonic() > deadline:
                raise RuntimeError(f"CRDs not established: {', '.join(sorted(pending))}")
            if pending:
                time.sleep(0.25)

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
