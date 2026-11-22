import ast, json, glob, os, sys, collections, coverage
ROOT = os.environ.get("COV_ROOT", "/work/sqlglot") + "/"
COVDIR = os.environ.get("COV_DIR", "/work/covdata")
OUT = "/work/br/map.json"
tests = json.load(open("/work/br/sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in tests}

cov = coverage.CoverageData("/tmp/cov_all"); 
cov.update(coverage.CoverageData("/work/covdata/.cov.gw0")) if False else None
# merge manually
lines = collections.defaultdict(lambda: collections.defaultdict(set))  # file -> line -> {test}
miss = set()
for cf in glob.glob(f"{COVDIR}/.cov.*"):
    d = coverage.CoverageData(cf); d.read()
    for f in d.measured_files():
        rel = os.path.relpath(f, ROOT)
        for ctx, ls in d.contexts_by_lineno(f).items():
            pass
        for ln, ctxs in d.contexts_by_lineno(f).items():
            for c in ctxs:
                if c == "": continue
                if c in num: lines[rel][ln].add(num[c])
                else: miss.add(c)
print("unmatched contexts", len(miss), list(miss)[:3], file=sys.stderr)

extra = collections.defaultdict(lambda: collections.defaultdict(set))
eager = set()
ecls = collections.defaultdict(set)
seen_tests = set()
for ef in glob.glob(f"{COVDIR}/eager.*.json"):
    eager |= set(json.load(open(ef)))
for ef in glob.glob(f"{COVDIR}/extra.*.json"):
    e = json.load(open(ef))
    tid = e["test"]
    if tid not in num: continue
    seen_tests.add(tid)
    for k in ("opens", "mods", "cls"):
        for x in e.get(k, ()): extra[k][x].add(num[tid])
    for x in e.get("ecls", ()): ecls[x].add(num[tid])
print("tests with data", len(seen_tests), "of", len(num), file=sys.stderr)

def modfile(m):
    p = m.replace(".", "/")
    for c in (p + ".py", p + "/__init__.py"):
        if os.path.exists(ROOT + c): return c
    return None

srcfiles = [os.path.relpath(p, ROOT) for p in glob.glob(ROOT + "sqlglot/**/*.py", recursive=True)]
M = {"files": {}, "eager": sorted(filter(None, (modfile(m) for m in eager)))}
setids = {}; sets = []
def sid(s):
    k = tuple(sorted(s))
    if k not in setids: setids[k] = len(sets); sets.append(list(k))
    return setids[k]
for rel in srcfiles:
    src = open(ROOT + rel).read()
    tree = ast.parse(src)
    funcs = []
    def visit(n, infunc):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and not infunc:
                start = min([c.lineno] + [d.lineno for d in c.decorator_list])
                funcs.append([start, c.body[0].lineno, c.end_lineno])
                continue
            visit(c, infunc)
    visit(tree, False)
    ln = lines.get(rel, {})
    allt = set()
    for s in ln.values(): allt |= s
    M["files"][rel] = {
        "funcs": funcs,
        "lines": {str(l): sid(s) for l, s in ln.items()},
        "all": sorted(allt),
    }
M["sets"] = sets
M["ecls"] = {k: sorted(v) for k, v in ecls.items()}
M["cls"] = {modfile(k) or k: sorted(v) for k, v in extra["cls"].items()}
M["mods"] = {modfile(k) or k: sorted(v) for k, v in extra["mods"].items()}
M["opens"] = {k: sorted(v) for k, v in extra["opens"].items() if not k.startswith("sqlglot/") or not k.endswith(".py")}
M["opens_py"] = {k: sorted(v) for k, v in extra["opens"].items() if k.endswith(".py")}
json.dump(M, open(OUT, "w"), separators=(",", ":"))
print(os.path.getsize(OUT), "bytes", len(sets), "sets", len(M["eager"]), "eager", file=sys.stderr)

# ---- extras: func cols, string spans, importers, fixture/test-file maps, source zip
import zipfile, re
def modname(rel):
    m = rel[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m
mod2file = {modname(r): r for r in srcfiles}
imports = {}
for rel in srcfiles:
    tree = ast.parse(open(ROOT + rel).read())
    pkg = modname(rel) if rel.endswith("__init__.py") else modname(rel).rpartition(".")[0]
    deps = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in mod2file: deps.add(mod2file[a.name])
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")
                parts = parts[: len(parts) - (n.level - 1)]
                base = ".".join(parts + ([n.module] if n.module else []))
            if base in mod2file: deps.add(mod2file[base])
            for a in n.names:
                if base + "." + a.name in mod2file: deps.add(mod2file[base + "." + a.name])
    imports[rel] = deps
importers = collections.defaultdict(set)
for r, ds in imports.items():
    for d in ds: importers[d].add(r)
def closure(f):
    seen = set(); st = [f]
    while st:
        x = st.pop()
        for y in importers.get(x, ()):
            if y not in seen: seen.add(y); st.append(y)
    return seen
for rel in srcfiles:
    M["files"][rel]["clos"] = sorted(closure(rel))
    src = open(ROOT + rel).read(); tree = ast.parse(src)
    M["files"][rel]["str"] = [[n.lineno, n.end_lineno] for n in ast.walk(tree) if ((isinstance(n, ast.Constant) and isinstance(n.value, str)) or isinstance(n, ast.JoinedStr)) and n.end_lineno > n.lineno]
    M["files"][rel]["glob"] = sorted({l for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal)) for l in range(n.lineno, n.end_lineno + 1)})
    stm = []
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) or isinstance(n, ast.ExceptHandler):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                continue  # docstring / bare string: no line coverage, handled conservatively
            body = getattr(n, "body", None)
            if isinstance(n, ast.stmt) and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # headers are import-time
            if isinstance(body, list) and body:
                stm.append([n.lineno, max(n.lineno, body[0].lineno - 1)])
            else:
                stm.append([n.lineno, n.end_lineno])
    stm.sort()
    M["files"][rel]["stm"] = stm
    if rel.startswith("sqlglot/expressions/"):
        import importlib
        sys.path.insert(0, ROOT)
        mod = importlib.import_module(modname(rel))
        from sqlglot import exp as _exp
        spans = []
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                cobj = getattr(mod, n.name, None)
                isexpr = isinstance(cobj, type) and issubclass(cobj, _exp.Expr) and cobj.__module__ == mod.__name__
                start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                spans.append([start, n.end_lineno, mod.__name__ + ":" + n.name, 1 if isexpr else 0])
        M["files"][rel]["classes"] = spans
    cols = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)): cols[n.lineno] = n.col_offset
    for f in M["files"][rel]["funcs"]:
        f.append(min(cols.get(l, 0) for l in range(f[0], f[1] + 1) if l in cols) if any(l in cols for l in range(f[0], f[1] + 1)) else 0)
# test files: which import which sqlglot file / other test file; fixtures static
testpy = [os.path.relpath(p, ROOT) for p in glob.glob(ROOT + "tests/**/*.py", recursive=True)]
tsrc = {p: open(ROOT + p).read() for p in testpy}
M["test_imports"] = collections.defaultdict(list)
M["test_importers"] = collections.defaultdict(list)
for p, s in tsrc.items():
    tree = ast.parse(s)
    for n in ast.walk(tree):
        names = []
        if isinstance(n, ast.Import): names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            names = [n.module] + [n.module + "." + a.name for a in n.names]
        for nm in names:
            if nm in mod2file: M["test_imports"][mod2file[nm]].append(p)
            tp = nm.replace(".", "/") + ".py"
            if tp in tsrc and tp != p: M["test_importers"][tp].append(p)
M["test_imports"] = {k: sorted(set(v)) for k, v in M["test_imports"].items()}
M["test_importers"] = {k: sorted(set(v)) for k, v in M["test_importers"].items()}
fx = {}
for root, _, fs in os.walk(ROOT + "tests/fixtures"):
    for f in fs:
        rel = os.path.relpath(os.path.join(root, f), ROOT)
        short = os.path.relpath(os.path.join(root, f), ROOT + "tests/fixtures")
        users = sorted(p for p, s in tsrc.items() if short in s or f in s)
        if users: fx[rel] = users
M["fixture_static"] = fx
M["fixture_readers"] = sorted(p for p, s in tsrc.items() if "fixtures" in s.lower() or "FIXTURES_DIR" in s)
IC = json.load(open(os.environ.get("IMPORT_COV_OUT", "/work/import_cov.json")))
for rel, info in M["files"].items():
    bare = set(IC["bare"].get(rel, ()))
    body = set()
    for fn in info["funcs"]:
        body |= set(range(fn[1], fn[2] + 1))
    info["imp"] = sorted(bare & body)
tm = collections.defaultdict(lambda: collections.defaultdict(list))
for tf, d in IC["testmod"].items():
    for rel, ls in d.items():
        for l in ls:
            tm[rel][str(l)].append(tf)
M["testmod"] = {k: dict(v) for k, v in tm.items()}
json.dump(M, open(OUT, "w"), separators=(",", ":"))
with zipfile.ZipFile("/work/br/src.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for r in srcfiles: z.write(ROOT + r, r)
print(os.path.getsize(OUT), os.path.getsize("/work/br/src.zip"), file=sys.stderr)
