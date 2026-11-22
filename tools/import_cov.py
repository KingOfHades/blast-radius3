"""Lines of sqlglot that run at import time.

bare: importing every sqlglot module and creating each dialect's tokenizer, parser
and generator once. A change to such a line acts like a module-level change.
per test file: extra sqlglot lines run while that test module is imported
(collection). A change there can break every test in that file.
"""
import json
import os
import subprocess
import sys

ROOT = os.environ.get("COV_ROOT", "/work/sqlglot")
OUT = os.environ.get("IMPORT_COV_OUT", "/work/import_cov.json")
PY = sys.executable

CHILD = r'''
import coverage, importlib, json, os, pkgutil, sys
root, mode, target, out = sys.argv[1:5]
sys.path.insert(0, root)
os.chdir(root)
cov = coverage.Coverage(data_file=None, source=[root + "/sqlglot"])
cov.start()
if mode == "bare":
    import sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    from sqlglot.dialects.dialect import Dialect, Dialects
    for d in Dialects:
        if not d.value:
            continue
        try:
            dia = Dialect.get_or_raise(d.value)
            dia.tokenizer_class()
            dia.parser_class()
            dia.generator_class()
            dia.tokenize("SELECT 1")
            dia.parse("SELECT 1")
            dia.generate(dia.parse("SELECT 1")[0])
        except Exception:
            pass
else:
    try:
        importlib.import_module(target)
    except Exception as e:
        sys.stderr.write(f"import error {target}: {e!r}\n")
cov.stop()
data = cov.get_data()
res = {}
for f in data.measured_files():
    rel = os.path.relpath(f, root)
    res[rel] = sorted(data.lines(f) or [])
json.dump(res, open(out, "w"))
'''


def run(mode, target, out):
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=ROOT)
    r = subprocess.run([PY, "-c", CHILD, ROOT, mode, target, out], env=env, capture_output=True, text=True, timeout=600)
    if r.returncode:
        sys.stderr.write(r.stderr[-2000:])
    return json.load(open(out))


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    files = sorted({t["file"] for t in tests})
    bare = run("bare", "", "/tmp/_ic_bare.json")
    per = {}
    for f in files:
        mod = f[:-3].replace("/", ".")
        got = run("test", mod, "/tmp/_ic_t.json")
        extra = {}
        for rel, ls in got.items():
            d = sorted(set(ls) - set(bare.get(rel, ())))
            if d:
                extra[rel] = d
        per[f] = extra
        print(f, sum(len(v) for v in extra.values()), file=sys.stderr)
    json.dump({"bare": bare, "testmod": per}, open(OUT, "w"))


if __name__ == "__main__":
    main()
