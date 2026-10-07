"""Generate an assets-keyed test manifest for one subject area.

Keying rationale, measured over the 7 weeks since PR 920 opened:
  content/docs/  2232 files, 89% changed path
  assets/agw-docs 478 files,  2% changed path
So a scenario is named by the assets file it reuses wherever one exists, and
falls back to a version-relative content path only when the page carries its
own body.
"""
import subprocess, yaml, pathlib, re, collections, sys

AREA = sys.argv[1] if len(sys.argv) > 1 else "traffic-management"
MODE = sys.argv[2] if len(sys.argv) > 2 else "kubernetes"
LIVE = ("latest", "main")

pages = {}
for p in subprocess.run(["git","ls-files","content/docs"],capture_output=True,text=True).stdout.split():
    if p.endswith((".md",".txt")):
        try: pages[p] = pathlib.Path(p).read_text(encoding="utf-8")
        except Exception: pass

def wrapper_source(path):
    t = pages.get(path)
    if not t: return None
    parts = t.split("---", 2)
    body = parts[2] if len(parts) >= 3 else t
    if len(re.sub(r'\{\{[<%].*?[>%]\}\}', '', body, flags=re.S).strip()) >= 200: return None
    r = re.findall(r'\{\{[<%]\s*reuse\s+"([^"]+)"', body)
    return r[0] if len(r) == 1 else None

# ---- collect scenarios from the live trees --------------------------------
raw = {}                      # name -> list of (vroot, page, type, steps)
for p, t in pages.items():
    parts = p.split("/")
    if len(parts) < 5 or parts[3] not in LIVE: continue
    if parts[2] != MODE: continue          # scenario names repeat across modes
    if AREA not in p: continue
    if not t.startswith("---"): continue
    try: fm = yaml.safe_load(t.split("---",2)[1]) or {}
    except Exception: continue
    d = fm.get("test") if isinstance(fm, dict) else None
    if not isinstance(d, dict): continue
    vroot = "/".join(parts[:4])
    for name, body in d.items():
        ty = body.get("type") if isinstance(body, dict) else None
        steps = body.get("steps") if isinstance(body, dict) else body
        raw.setdefault(name, []).append((vroot, p, ty, steps or []))

# ---- name the repeated prerequisites once ---------------------------------
PREREQ_NAMES = {
    ("documentation/quickstart/install.md", "standard"): "install-standard",
    ("documentation/quickstart/install.md", "experimental"): "install-experimental",
    ("documentation/install/helm.md", "standard"): "helm-standard",
    ("documentation/install/helm.md", "experimental"): "helm-experimental",
    ("documentation/setup/gateway.md", "all"): "gateway",
    ("documentation/install/sample-app.md", "install-httpbin"): "httpbin",
}
used_prereqs = {}

def step_ref(vroot, decl_page, s):
    """One step -> either a named prerequisite, or an inline {source|page, path}."""
    f, sel = s.get("file"), s.get("path")
    rel = f.replace("${versionRoot}/", "") if f else decl_page.split(f"{vroot}/")[-1]
    key = (rel, sel)
    if key in PREREQ_NAMES:
        used_prereqs[PREREQ_NAMES[key]] = {"page": rel, "path": sel}
        return PREREQ_NAMES[key]
    target = f.replace("${versionRoot}", vroot) if f else decl_page
    src = wrapper_source(target)
    ref = {"source": src} if src else {"page": rel}
    if sel: ref["path"] = sel
    if s.get("assert"): ref["assert"] = list(s["assert"])
    return ref

scenarios, skew = collections.OrderedDict(), []
for name in sorted(raw):
    variants = raw[name]
    rendered = []
    for vroot, page, ty, steps in variants:
        rendered.append((ty, [step_ref(vroot, page, s) for s in steps]))
    first = rendered[0]
    if any(r != first for r in rendered[1:]):
        skew.append(name)          # latest and main disagree; keep both, flag it
    ty, refs = first
    entry = {}
    if ty is not None: entry["type"] = ty
    needs = [r for r in refs[:-1] if isinstance(r, str)]
    inline_pre = [r for r in refs[:-1] if not isinstance(r, str)]
    if needs: entry["needs"] = needs
    if inline_pre: entry["before"] = inline_pre
    last = refs[-1] if refs else None
    if isinstance(last, dict):
        entry.update({k: v for k, v in last.items()})
    elif last:
        entry.setdefault("needs", []).append(last)
    scenarios[name] = entry

out = {
    "version": 1,
    "mode": MODE,
    "prerequisites": {k: used_prereqs[k] for k in sorted(used_prereqs)},
    "scenarios": dict(scenarios),
}
print(f"area={AREA} mode={MODE}", file=sys.stderr)
print(f"  scenarios          : {len(scenarios)} (from {sum(len(v) for v in raw.values())} front-matter copies across {len(LIVE)} version roots)", file=sys.stderr)
print(f"  named prerequisites: {len(used_prereqs)}", file=sys.stderr)
print(f"  latest/main skew   : {len(skew)} {skew[:5]}", file=sys.stderr)
yaml.safe_dump(out, sys.stdout, sort_keys=False, default_flow_style=False, width=100)
