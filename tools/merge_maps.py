import json, sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2])); out = sys.argv[3]
sets = []; ids = {}
def sid(s):
    k = tuple(sorted(s))
    if k not in ids: ids[k] = len(sets); sets.append(list(k))
    return ids[k]
M = dict(b)
for f, info in b["files"].items():
    ai = a["files"].get(f, {"lines": {}, "all": []})
    lines = {}
    for l in set(info["lines"]) | set(ai["lines"]):
        s = set()
        if l in info["lines"]: s |= set(b["sets"][info["lines"][l]])
        if l in ai["lines"]: s |= set(a["sets"][ai["lines"][l]])
        lines[l] = sid(s)
    info["lines"] = lines
    info["all"] = sorted(set(info["all"]) | set(ai["all"]))
M["sets"] = sets
for f, info in b["files"].items():
    pass
for k in ("cls", "mods", "opens", "opens_py", "ecls"):
    d = {x: set(v) for x, v in b.get(k, {}).items()}
    for x, v in a.get(k, {}).items(): d.setdefault(x, set()).update(v)
    M[k] = {x: sorted(v) for x, v in d.items()}
json.dump(M, open(out, "w"), separators=(",", ":"))
