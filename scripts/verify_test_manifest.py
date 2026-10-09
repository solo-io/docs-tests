"""Expand the generated manifest back into test cases and diff against front matter."""
import subprocess, yaml, pathlib, re, collections, sys

AREA, MODE = "traffic-management", sys.argv[1]
LIVE = ("latest", "main")
SP = "/private/tmp/claude-501/-Users-kristinbrown-Documents-GitHub/22da2a7a-3ec7-4e02-9ea7-09e33dda5a31/scratchpad"
man = yaml.safe_load(open(f"{SP}/tests-{MODE}.yaml"))

pages = {}
for p in subprocess.run(["git","ls-files","content/docs"],capture_output=True,text=True).stdout.split():
    if p.endswith((".md",".txt")):
        try: pages[p] = pathlib.Path(p).read_text(encoding="utf-8")
        except Exception: pass

def wrapper_source(path):
    t = pages.get(path)
    if not t: return None
    parts = t.split("---",2); body = parts[2] if len(parts)>=3 else t
    if len(re.sub(r'\{\{[<%].*?[>%]\}\}','',body,flags=re.S).strip()) >= 200: return None
    r = re.findall(r'\{\{[<%]\s*reuse\s+"([^"]+)"', body)
    return r[0] if len(r)==1 else None

# assets source -> content page, per version root
src_to_page = collections.defaultdict(dict)
for p in pages:
    parts = p.split("/")
    if len(parts) < 5 or parts[2] != MODE or parts[3] not in LIVE: continue
    s = wrapper_source(p)
    if s: src_to_page[parts[3]][s] = p

def resolve(ref, root):
    """manifest ref -> (file-token, path) as front matter would have written it."""
    if isinstance(ref, str):
        pre = man["prerequisites"][ref]
        return ("${versionRoot}/" + pre["page"], pre.get("path"))
    if "source" in ref:
        page = src_to_page[root].get(ref["source"])
        if not page: return (None, ref.get("path"))
        rel = page.split(f"content/docs/{MODE}/{root}/",1)[1]
        return ("${versionRoot}/" + rel, ref.get("path"))
    return ("${versionRoot}/" + ref["page"], ref.get("path"))

# ---- expand the manifest ---------------------------------------------------
expanded = {}
for name, e in man["scenarios"].items():
    for root in LIVE:
        steps = []
        for n in e.get("needs", []): steps.append(resolve(n, root))
        for b in e.get("before", []): steps.append(resolve(b, root))
        own = {k: v for k, v in e.items() if k in ("source","page","path","assert")}
        if own: steps.append(resolve(own, root))
        expanded[(root, name)] = (e.get("type"), steps)

# ---- what the front matter says today --------------------------------------
actual = {}
for p, t in pages.items():
    parts = p.split("/")
    if len(parts) < 5 or parts[2] != MODE or parts[3] not in LIVE: continue
    if AREA not in p or not t.startswith("---"): continue
    try: fm = yaml.safe_load(t.split("---",2)[1]) or {}
    except Exception: continue
    d = fm.get("test") if isinstance(fm, dict) else None
    if not isinstance(d, dict): continue
    for name, body in d.items():
        ty = body.get("type") if isinstance(body, dict) else None
        steps = body.get("steps") if isinstance(body, dict) else body
        norm = []
        for s in (steps or []):
            f = s.get("file") or ("${versionRoot}/" + p.split(f"content/docs/{MODE}/{parts[3]}/",1)[1])
            norm.append((f, s.get("path")))
        actual[(parts[3], name)] = (ty, norm)

only_m = sorted(set(expanded) - set(actual))
only_a = sorted(set(actual) - set(expanded))
diff = [k for k in set(expanded) & set(actual) if expanded[k] != actual[k]]
print(f"=== round-trip, mode={MODE} ===")
print(f"  test cases from manifest    : {len(expanded)}")
print(f"  test cases from front matter: {len(actual)}")
print(f"  only in manifest            : {len(only_m)} {only_m[:3]}")
print(f"  only in front matter        : {len(only_a)} {only_a[:3]}")
print(f"  present in both but DIFFER  : {len(diff)}")
for k in diff[:3]:
    print(f"     {k}\n       manifest   : {expanded[k]}\n       frontmatter: {actual[k]}")
