"""Blast Radius selector: per-test line coverage map (built from the pinned sqlglot commit).

For each patch we find what changed and select every test that could execute it.
Anything we cannot reason about falls back to running every test (always safe).
"""

import json
import os
import time
import re
import sys
import zipfile
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
MAX_WORKERS = 8
WORKER_MS = 2000

HUB = {
    "sqlglot/__init__.py", "sqlglot/_typing.py", "sqlglot/helper.py", "sqlglot/errors.py",
    "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py",
    "sqlglot/dialects/dialect.py", "sqlglot/dialects/__init__.py", "sqlglot/parsers/base.py",
    "sqlglot/parsers/__init__.py", "sqlglot/generators/__init__.py", "sqlglot/typing/__init__.py",
    "sqlglot/trie.py", "sqlglot/time.py", "sqlglot/schema.py", "sqlglot/jsonpath.py",
}
FAMILY_DIRS = ("dialects", "parsers", "generators", "typing")
ALL_CONFIG = re.compile(
    r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|setup\.py|setup\.cfg|pyproject\.toml|"
    r"MANIFEST\.in|Makefile|requirements[^/]*\.(txt|lock)|\.coveragerc)$"
)
BLANK_OR_COMMENT = re.compile(r"^\s*(#.*)?$")
FULL_COMMENT = re.compile(r"^\s*#")


class All(Exception):
    pass


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- patch parsing
def parse_patch(text):
    files = []
    cur = None
    hunk = None
    old_no = 0
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", raw)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None,
                   "hunks": [], "new_file": False, "deleted": False, "binary": False, "rename": False}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if hunk is None:
            if raw.startswith("new file mode"):
                cur["new_file"] = True
            elif raw.startswith("deleted file mode"):
                cur["deleted"] = True
            elif raw.startswith("rename from") or raw.startswith("rename to") or raw.startswith("copy "):
                cur["rename"] = True
            elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
                cur["binary"] = True
            elif raw.startswith("--- "):
                p = raw[4:].strip()
                if p == "/dev/null":
                    cur["new_file"] = True
                elif p.startswith("a/"):
                    cur["old"] = p[2:]
            elif raw.startswith("+++ "):
                p = raw[4:].strip()
                if p == "/dev/null":
                    cur["deleted"] = True
                elif p.startswith("b/"):
                    cur["new"] = p[2:]
        if raw.startswith("@@") and (hunk is None or (hunk["left_o"] <= 0 and hunk["left_n"] <= 0)):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw)
            if not m:
                raise All("bad hunk")
            ol = int(m.group(2)) if m.group(2) is not None else 1
            nl = int(m.group(4)) if m.group(4) is not None else 1
            hunk = {"os": int(m.group(1)), "ol": ol, "lines": [], "left_o": ol, "left_n": nl}
            cur["hunks"].append(hunk)
            continue
        if hunk is not None and (hunk["left_o"] > 0 or hunk["left_n"] > 0):
            tag = raw[:1]
            if raw == "":
                tag, raw = " ", " "  # blank context line whose leading space was stripped
            if tag == " ":
                hunk["lines"].append((" ", raw[1:]))
                hunk["left_o"] -= 1
                hunk["left_n"] -= 1
            elif tag == "-":
                hunk["lines"].append(("-", raw[1:]))
                hunk["left_o"] -= 1
            elif tag == "+":
                hunk["lines"].append(("+", raw[1:]))
                hunk["left_n"] -= 1
    return files


def hunk_changes(hunk):
    """Groups of consecutive changed lines: (removed[(old_lineno, text)], added[text], prev_old, next_old)."""
    groups = []
    o = hunk["os"]
    cur = None
    for tag, text in hunk["lines"]:
        if tag == " ":
            if cur:
                cur[3] = o
                groups.append(tuple(cur))
                cur = None
            o += 1
        else:
            if cur is None:
                cur = [[], [], o - 1, o]
            if tag == "-":
                cur[0].append((o, text))
                o += 1
            else:
                cur[1].append(text)
    if cur:
        cur[3] = o
        groups.append(tuple(cur))
    return groups


def apply_to_region(old_lines, hunks, lo, hi):
    """New text of old lines lo..hi (1-based, inclusive) after applying hunks that fall inside."""
    new = []
    mapping = []  # new index -> old line (for context lines) or None
    pos = 1
    for h in hunks:
        while pos < h["os"]:
            new.append(old_lines[pos - 1])
            mapping.append(pos)
            pos += 1
        for tag, text in h["lines"]:
            if tag == " ":
                new.append(old_lines[pos - 1])
                mapping.append(pos)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                new.append(text)
                mapping.append(None)
    while pos <= len(old_lines):
        new.append(old_lines[pos - 1])
        mapping.append(pos)
        pos += 1
    return new, mapping


# ---------------------------------------------------------------- selection
class Selector:
    def __init__(self, tests):
        self.M = json.loads((APP / "map.json").read_text())
        self.zip = zipfile.ZipFile(APP / "src.zip")
        self.tests = tests
        self.all = {t["number"] for t in tests}
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self.sets = self.M["sets"]
        self.cache = {}
        self.lazy = {t["number"] for t in tests if t["id"].endswith("::test_lazy_load")}

    def tests_of_files(self, files):
        out = set()
        for f in files:
            out |= self.by_file.get(f, set())
        return out

    def func_tests(self, rel, start, end):
        key = (rel, start, end)
        if key in self.cache:
            return self.cache[key]
        lines = self.M["files"][rel]["lines"]
        out = set()
        seen = set()
        for ln, sid in lines.items():
            if start <= int(ln) <= end and sid not in seen:
                seen.add(sid)
                out.update(self.sets[sid])
        self.cache[key] = out
        return out

    def file_tests(self, rel):
        return set(self.M["files"][rel]["all"])

    def import_time_tests(self, rel, lines=()):
        M = self.M
        if rel.startswith("sqlglot/expressions/") and rel in M["files"] and "classes" in M["files"][rel]:
            keys = set()
            for l in lines:
                hit = None
                for a, b, key, isexpr in M["files"][rel]["classes"]:
                    if a <= l <= b:
                        hit = (key, isexpr)
                        break
                if hit is None or not hit[1]:
                    raise All("module-level/non-Expr change in " + rel)
                keys.add(hit[0])
            out = set(self.tests_of_files(["tests/test_expressions.py", "tests/test_build.py"]))
            for k in keys:
                out |= set(M["ecls"].get(k, ()))
            return out
        parts = rel.split("/")
        base = parts[-1][:-3]
        if len(parts) == 3 and parts[1] in FAMILY_DIRS and rel not in HUB and base != "__init__":
            out = set(self.file_tests(rel))
            for d in FAMILY_DIRS:
                sib = f"sqlglot/{d}/{base}.py"
                if sib in M["files"]:
                    out |= self.file_tests(sib)
                    out |= set(M["cls"].get(sib, ()))
                    out |= set(M["mods"].get(sib, ()))
            out |= set(M["cls"].get(rel, ()))
            out |= set(M["mods"].get(rel, ()))
            out |= self.tests_of_files(M["test_imports"].get(rel, ()))
            # dialects that inherit from this one are covered through cls (MRO)
            return out
        raise All("import-time change in " + rel)

    def source_change(self, f):
        M = self.M
        rel = f["new"] if not f["deleted"] else f["old"]
        if f["deleted"] or f["rename"] or f["old"] != f["new"]:
            raise All("delete/rename")
        if f["new_file"]:
            # a brand new module: nothing runs it unless a changed file imports it
            return self.tests_of_files(["tests/test_dialect_imports.py", "tests/test_dialect_entry_points.py", "tests/test_build.py"])
        info = M["files"].get(rel)
        if info is None:
            raise All("unknown source file")
        old_text = self.zip.read(rel).decode("utf-8")
        old_lines = old_text.split("\n")
        strspans = info["str"]
        funcs = info["funcs"]

        def in_string(l):
            for a, b in strspans:
                if a < l <= b:
                    return True
            return False

        def gap_in_string(prev):
            for a, b in strspans:
                if a <= prev < b:
                    return True
            return False

        def func_of(l):
            for fn in funcs:
                if fn[1] <= l <= fn[2]:
                    return fn
            return None

        sel = set()
        structural = False
        import_lines = []
        func_hits = {}
        guard = {}
        stm = info["stm"]
        globs = set(info.get("glob", ()))
        lines_map = info["lines"]

        def stmt_tests(l):
            """Tests that execute the statement containing old line l, or None if unmapped."""
            best = None
            for sp in stm:
                if sp[0] > l:
                    break
                if sp[1] >= l and (best is None or sp[0] >= best[0]):
                    best = sp
            if best is None:
                return None
            return self.func_tests(rel, best[0], best[1])

        def indent_of(t):
            return len(t) - len(t.lstrip())

        for h in f["hunks"]:
            for removed, added, prev, nxt in hunk_changes(h):
                rem = [(o, t) for o, t in removed if not (BLANK_OR_COMMENT.match(t) and not in_string(o))]
                for o, t in removed:
                    if FULL_COMMENT.match(t) and not in_string(o) and o > 1 and old_lines[o - 2].rstrip().endswith("\\"):
                        structural = True
                add = [t for t in added if t.strip() and not (FULL_COMMENT.match(t) and not gap_in_string(prev))]
                if not rem and not add:
                    if any(t.strip() and FULL_COMMENT.match(t) for t in added) and prev >= 1 and prev <= len(old_lines) \
                            and old_lines[prev - 1].rstrip().endswith("\\"):
                        structural = True
                    if added and gap_in_string(prev):
                        structural = True
                        fn = func_of(prev)
                        if fn:
                            func_hits[(fn[0], fn[2])] = fn
                        else:
                            import_lines.append(prev)
                    continue
                if rem and len(added) == len(removed) and all(a_.rstrip() == r[1].rstrip() for a_, r in zip(added, removed)) \
                        and not any(in_string(o) for o, _ in removed):
                    continue  # trailing-whitespace-only edit
                structural = True
                # removed / replaced statements
                line_level = bool(rem)
                for o, t in rem:
                    fn = func_of(o)
                    if fn is None:
                        import_lines.append(o)
                        line_level = False
                        continue
                    tt = None if (in_string(o) or o in globs) else stmt_tests(o)
                    if tt is None:
                        func_hits[(fn[0], fn[2])] = fn
                        line_level = False
                    else:
                        sel |= tt
                        guard[(fn[0], fn[2])] = fn
                if rem and add:
                    rmin = min(indent_of(t) for _, t in rem)
                    if any(indent_of(t) < rmin for t in add):
                        line_level = False
                        for o, t in rem:
                            fn = func_of(o)
                            if fn:
                                func_hits[(fn[0], fn[2])] = fn
                elif add:
                    # pure insertion
                    a = func_of(prev) if prev >= 1 else None
                    b = func_of(nxt)
                    if a is not None and b is not None and a is b:
                        func_hits[(a[0], a[2])] = a
                        continue
                    cand = a or b
                    if cand is not None:
                        first = next((t for t in add), "")
                        if indent_of(first) > cand[3]:
                            func_hits[(cand[0], cand[2])] = cand
                            continue
                    import_lines.extend([prev, nxt])
        # functions that run while modules are imported act like module-level code
        imp = set(info.get("imp", ()))
        tmod = M.get("testmod", {}).get(rel, {})
        for key, fn in list(func_hits.items()) + list(guard.items()):
            rng = range(fn[1], fn[2] + 1)
            if imp and any(l in imp for l in rng):
                if rel.startswith("sqlglot/expressions/"):
                    raise All("import-executed function in " + rel)
                import_lines.append(fn[1])
            for l in rng:
                for tf in tmod.get(str(l), ()):
                    sel |= self.by_file.get(tf, set())
        import_time = bool(import_lines)
        touched = set()
        for key, fn in list(func_hits.items()) + list(guard.items()):
            touched |= set(range(fn[0], fn[2] + 1))
        self.touched[rel] = None if import_time else touched
        if not structural:
            # pure comment/blank edits: nothing executes differently, as long as the file still
            # compiles the same way (no encoding cookie, valid syntax)
            new_text, _ = apply_to_region(old_lines, f["hunks"], 1, len(old_lines))
            import ast as _ast
            for h in f["hunks"]:
                for tag, t in h["lines"]:
                    if tag in "+-" and re.match(r"^[ \t\f]*#.*?coding[:=]", t):
                        raise All("encoding cookie")
            try:
                _ast.parse("\n".join(new_text))
            except SyntaxError:
                raise All("syntax error")
            return set(M["opens_py"].get(rel, ()))
        # syntax guard on the changed functions / whole file for import-time edits
        new, mapping = apply_to_region(old_lines, f["hunks"], 1, len(old_lines))
        import ast
        if import_time:
            try:
                ast.parse("\n".join(new))
            except SyntaxError:
                raise All("syntax error")
            sel |= self.import_time_tests(rel, import_lines) | self.lazy
        else:
            # check each hit function's new text parses
            for (start, end), fn in list(func_hits.items()) + list(guard.items()):
                seg = self._new_segment(old_lines, f["hunks"], fn)
                try:
                    ast.parse(seg)
                except SyntaxError:
                    raise All("syntax error in function")
        for (start, end), fn in func_hits.items():
            sel |= self.func_tests(rel, fn[0], fn[2])
            # decorators/signature lines are import-time but we only get here for body lines
        return sel

    def _new_segment(self, old_lines, hunks, fn):
        start, body, end = fn[0], fn[1], fn[2]
        new, mapping = apply_to_region(old_lines, hunks, 1, len(old_lines))
        # locate segment in new text via mapping of unchanged boundary lines
        try:
            s = mapping.index(start)
        except ValueError:
            raise All("def line changed")
        e = None
        for i in range(len(mapping) - 1, -1, -1):
            if mapping[i] is not None and mapping[i] <= end:
                e = i
                break
        # include inserted lines right after the last old line of the function
        j = e + 1
        col = fn[3]
        while j < len(new) and mapping[j] is None and (new[j].strip() == "" or len(new[j]) - len(new[j].lstrip()) > col):
            j += 1
        seg = new[s:j]
        import textwrap
        return textwrap.dedent("\n".join(seg)) + "\n"

    def fixture_change(self, f):
        M = self.M
        rel = f["new"] if not f["deleted"] else f["old"]
        out = set(M["opens"].get(rel, ()))
        if not out or not rel.endswith(".sql"):
            out |= self.tests_of_files(M["fixture_static"].get(rel, ()))
        if not out:
            out = self.tests_of_files(M["fixture_readers"])
        return out

    def test_file_change(self, f):
        M = self.M
        rel = f["new"] if not f["deleted"] else f["old"]
        if rel in ("tests/helpers.py", "tests/__init__.py", "tests/dialects/__init__.py") or rel.endswith("conftest.py"):
            raise All("test helper")
        out = set(self.by_file.get(rel, ()))
        out |= self.tests_of_files(M["test_importers"].get(rel, ()))
        if not out and rel.endswith(".py") and not f["new_file"] and rel.split("/")[-1].startswith("test_"):
            pass
        return out

    def commit(self, patch_text):
        self.touched = {}
        files = parse_patch(patch_text)
        if not files:
            return set()
        sel = set()
        for f in files:
            rel = f["new"] if not f["deleted"] else f["old"]
            if rel is None:
                raise All("no path")
            if f["binary"] and not rel.startswith("tests/fixtures/"):
                raise All("binary")
            if ALL_CONFIG.search(rel):
                raise All("config file " + rel)
            if rel.startswith("sqlglot/") and rel.endswith(".py"):
                sel |= self.source_change(f)
            elif rel.startswith("tests/fixtures/"):
                sel |= self.fixture_change(f)
            elif rel.startswith("tests/") and rel.endswith(".py"):
                sel |= self.test_file_change(f)
            elif rel.startswith("tests/"):
                sel |= self.fixture_change(f)
            elif rel.startswith("sqlglot/"):
                raise All("non-py source " + rel)
            else:
                # docs, benchmarks, CI, etc.: only matters if a test reads the file
                sel |= set(self.M["opens"].get(rel, ()))
                if rel.endswith(".py") or rel.startswith("sqlglotc/"):
                    raise All("unknown code path " + rel)
        return sel


# ---------------------------------------------------------------- scheduling
def schedule(numbers, dur):
    if not numbers:
        return []
    items = sorted(numbers, key=lambda n: -dur[n])
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        buckets = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            buckets[i].append(n)
            loads[i] += dur[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, buckets)
    return [b for b in best[1] if b]


NOVERIFY_SUFFIX = ("::test_lazy_load",)
NOVERIFY_FILES = ("tests/test_sqlglotc.py",)
BUDGET = float(os.environ.get("BR_BUDGET", "150"))
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
VERIFY = os.environ.get("BR_VERIFY", "0") == "1"


def patched_texts(files, root):
    """New text for every file a patch touches, or None when we cannot rebuild it."""
    py, other = {}, {}
    for f in files:
        if f["new_file"] or f["deleted"] or f["rename"] or f["binary"] or f["old"] != f["new"]:
            return None
        rel = f["new"]
        try:
            with open(os.path.join(root, rel), encoding="utf-8", newline="") as fh:
                old = fh.read()
        except Exception:
            return None
        old_lines = old.split("\n")
        new, _ = apply_to_region(old_lines, f["hunks"], 1, len(old_lines))
        # check the hunks matched the original text
        for h in f["hunks"]:
            o = h["os"]
            for tag, text in h["lines"]:
                if tag in (" ", "-"):
                    if o - 1 >= len(old_lines) or old_lines[o - 1].rstrip("\r") != text.rstrip("\r"):
                        return None
                    o += 1
        text = "\n".join(new)
        (py if rel.endswith(".py") else other)[rel] = text
    return py, other


def _expand(rs):
    out = []
    for r in rs:
        if isinstance(r, list):
            out.extend(range(r[0], r[1] + 1))
        else:
            out.append(r)
    return out


def pack_cost(nums, dur):
    ws = schedule(set(nums), dur)
    return (max((sum(dur[x] for x in w) for w in ws), default=0) + WORKER_MS * len(ws)) / 1000


def _overhead(v, costs, file_of):
    per = {}
    for n in v:
        k = str(n)
        o = max(0.0, costs["solo"].get(k, 0.3) - costs["body"].get(k, 0.0))
        f = file_of[n]
        per[f] = max(per.get(f, 0.0), o)
    return sum(per.values()) + 0.03 * len(per)


def choose_verify(chosen, cand, est, dur, costs, file_of):
    """Which candidates to run: the set minimising (blind cost of the rest) + (CPU to run them),
    assuming the run tests pass. Tried by dropping the costliest tests from the run one by one."""
    best_v, best = set(), pack_cost(chosen, dur)
    order = sorted(cand, key=lambda n: est[n])
    k = len(order)
    while k > 0:
        v = order[:k]
        c = pack_cost(set(chosen) - set(v), dur) + sum(est[n] for n in v) + _overhead(v, costs, file_of)
        if c < best:
            best, best_v = c, set(v)
        k -= 1
    return best_v


CHEAP = float(os.environ.get("BR_CHEAP", "1.0"))
CAL = float(os.environ.get("BR_CAL", "1.15"))
ISOLATE = float(os.environ.get("BR_ISOLATE", "0.5"))


def add_jobs(jobs, key, pt, v, est, need, phase):
    import verify as V
    byfile = {}
    for n in sorted(v):
        if _COSTS["body"].get(str(n), 0) >= ISOLATE:
            byfile[("solo", n)] = [n]  # heavy tests run alone: their cost depends on what ran before
        else:
            byfile.setdefault(_FILE_OF[n], []).append(n)
    for f, ns in byfile.items():
        e = sum(est[n] for n in ns) + _overhead(ns, _COSTS, _FILE_OF)
        job = V.Job(key, pt[0], pt[1], [(n, _IDS[n]) for n in ns], budget=15 + 4 * e,
                    need={n: need[n] for n in ns if n in need})
        job.est = e
        jobs.append(job)


_FILE_OF, _COSTS, _IDS = {}, {}, {}


def main():
    t_start = time.monotonic()
    costs, subs = {}, {}
    if VERIFY:
        costs = json.loads((APP / "cost.json").read_text())
        with zipfile.ZipFile(APP / "sub.zip") as z:
            subs = json.loads(z.read("sub.json"))
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    file_of = {t["number"]: t["file"] for t in tests}
    _FILE_OF.update(file_of)
    _IDS.update(ids)
    _COSTS.update(costs)
    plans = {}
    noverify = {t["number"] for t in tests if t["id"].endswith(NOVERIFY_SUFFIX) or t["file"] in NOVERIFY_FILES}
    sel = Selector(tests)
    root = os.environ.get("BR_ROOT", "/opt/sqlglot")
    results = {}
    jobs = []
    fallbacks = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        text = patch.read_text(encoding="utf-8", errors="replace")
        verifiable = True
        try:
            chosen = sel.commit(text) & sel.all
        except All as e:
            log("ALL", patch.stem, e)
            chosen, verifiable = set(sel.all), False
            fallbacks += 1
        except Exception as e:  # never crash: run everything
            log("ERR", patch.stem, repr(e))
            chosen, verifiable = set(sel.all), False
            fallbacks += 1
        results[patch.stem] = chosen
        if not VERIFY or not verifiable or not chosen:
            continue
        try:
            pt = patched_texts(parse_patch(text), root)
        except Exception as e:
            log("patch-rebuild error", patch.stem, repr(e))
            pt = None
        if pt is None:
            continue
        opens_py = set()
        for rel in pt[0]:
            opens_py |= set(sel.M.get("opens_py", {}).get(rel, ()))
        cand = [n for n in chosen if n not in noverify and n not in opens_py]
        # which pool submissions each candidate really needs
        touched = sel.touched
        skippable = bool(touched) and all(v is not None for v in touched.values()) and \
            all(r.startswith("sqlglot/") for r in pt[0]) and not pt[1]
        need = {}
        est = {}
        for n in cand:
            base = costs["body"].get(str(n), dur[n] / 1000)
            info = subs.get(str(n))
            if info is None or not skippable:
                est[n] = base
                continue
            hit_main = False
            idx = set()
            for rel, lines in touched.items():
                main = info["main"].get(rel)
                if main and any(l in lines for l in _expand(main)):
                    hit_main = True
                    break
                for l, ix in info["sub"].get(rel, {}).items():
                    if int(l) in lines:
                        idx.update(_expand(ix))
            if hit_main:
                est[n] = base
                continue
            need[n] = idx
            est[n] = base * (0.08 + 0.92 * len(idx) / max(1, info["n"]))
        cheap = [n for n in cand if est[n] < CHEAP]
        exp_ = [n for n in cand if est[n] >= CHEAP]
        rest = set(chosen) - set(cheap)
        v1 = set()
        if cheap:
            c_blind = pack_cost(chosen, dur)
            c_ver = pack_cost(rest, dur) + sum(est[n] for n in cheap) * CAL + _overhead(cheap, costs, file_of)
            if c_ver < c_blind or exp_:
                v1 = set(cheap)
        plans[patch.stem] = {"pt": pt, "exp": exp_, "est": est, "need": need}
        if v1:
            add_jobs(jobs, patch.stem, pt, v1, est, need, phase=1)
        log(f"plan {patch.stem}: phase1 {len(v1)} cheap, {len(exp_)} costly, of {len(chosen)}")
    if plans:
        import verify as V
        for t in tests:
            V.limits[t["number"]] = 10 + 5 * t["seconds"]
        V.preload()
        # cheapest first, so a tight budget verifies as many commits as possible
        jobs.sort(key=lambda j: j.est)
        V.run_jobs(jobs, PARALLEL, t_start + BUDGET, int(3.4 * (1 << 30)), log)
        # phase 2: costly tests, only for commits where every cheap test passed
        failed_seen = {}
        for job in jobs:
            bad = {n for n, _ in job.tests} - (job.passed - job.failed)
            failed_seen[job.key] = failed_seen.get(job.key, False) or bool(bad)
        jobs2 = []
        for key, p in plans.items():
            if not p["exp"] or failed_seen.get(key):
                continue
            cur = set(results[key])
            for job in jobs:
                if job.key == key:
                    cur -= (job.passed - job.failed)
            v2 = set()
            for n in sorted(p["exp"], key=lambda n: -p["est"][n]):
                gain = pack_cost(cur, dur) - pack_cost(cur - {n}, dur)
                if p["est"][n] * CAL + 0.3 < gain:
                    v2.add(n)
                    cur.discard(n)
            if v2:
                add_jobs(jobs2, key, p["pt"], v2, p["est"], p["need"], phase=2)
                log(f"phase2 {key}: verify {len(v2)} costly")
        jobs2.sort(key=lambda j: j.est)
        V.run_jobs(jobs2, PARALLEL, t_start + BUDGET, int(3.4 * (1 << 30)), log)
        jobs += jobs2
        for job in jobs:
            ok = (job.passed - job.failed) & {n for n, _ in job.tests}
            before = len(results[job.key])
            results[job.key] = results[job.key] - ok
            log(f"verified {job.key}: {len(job.tests)} run, {len(ok)} pass, kept {len(results[job.key])} of {before}, cpu {job.cpu}")
            if os.environ.get("BR_DUMP"):
                with open(os.environ["BR_DUMP"], "a") as fh:
                    fh.write(json.dumps({"commit": job.key, "tests": [n for n, _ in job.tests], "passed": sorted(job.passed - job.failed),
                                         "tcpu": job.tcpu, "cpu": job.cpu}) + "\n")
    for key in sorted(results):
        print(json.dumps({"commit": key, "workers": schedule(results[key], dur)}))
    log("fallbacks:", fallbacks, "elapsed", round(time.monotonic() - t_start, 1))


if __name__ == "__main__":
    main()
