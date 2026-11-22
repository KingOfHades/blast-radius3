"""Per-test coverage with every test run in its own forked process.

Each test starts from the state right after collection: class/module setup runs
again for every test and no cache is shared, so code reached through setUpClass
or a warm cache is attributed to every test that depends on it.
"""
import json
import os
import sys

import coverage
import pytest

ROOT = os.environ.get("COV_ROOT", "/work/sqlglot") + "/"
COVDIR = os.environ.get("COV_DIR", "/work/covdata")
_cur = [None]
_opens = set()
_cls = set()
_ecls = set()


def _audit(ev, args):
    if ev == "open" and _cur[0] is not None:
        p = args[0]
        if isinstance(p, str) and p.startswith(ROOT) and not p.endswith(".pyc"):
            _opens.add(p[len(ROOT):])


def _wrap(cls, sink, prefix):
    orig = cls.__init__

    def init(self, *a, **k):
        if _cur[0] is not None:
            for c in type(self).__mro__:
                m = c.__module__
                if m.startswith(prefix):
                    sink.add(m if sink is _cls else m + ":" + c.__name__)
        return orig(self, *a, **k)

    cls.__init__ = init


class _InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            import pickle
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **k):
        import pickle
        from concurrent.futures import Future
        f = Future()
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return [fn(*a) for a in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def pytest_configure(config):
    import concurrent.futures as cf
    cf.ProcessPoolExecutor = _InlineExecutor
    sys.addaudithook(_audit)


def pytest_collection_finish(session):
    from sqlglot import exp, generator, parser, tokens
    from sqlglot.dialects import dialect
    for c in (parser.Parser, generator.Generator, tokens.Tokenizer, dialect.Dialect):
        _wrap(c, _cls, "sqlglot")
    _wrap(exp.Expr, _ecls, "sqlglot.expressions")
    eager = sorted(m for m in sys.modules if m.startswith("sqlglot"))
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"{COVDIR}/eager.{wid}.json", "w") as f:
        json.dump(eager, f)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            tag = f"{os.getpid()}"
            cov = coverage.Coverage(data_file=f"{COVDIR}/.cov.{tag}", source=[ROOT + "sqlglot"], data_suffix=False)
            before = set(m for m in sys.modules if m.startswith("sqlglot"))
            _cur[0] = item.nodeid
            cov.start()
            cov.switch_context(item.nodeid)
            reports = runtestprotocol(item, nextitem=None, log=False)
            cov.stop()
            cov.save()
            _cur[0] = None
            after = set(m for m in sys.modules if m.startswith("sqlglot"))
            out = {
                "test": item.nodeid,
                "failed": any(r.failed for r in reports),
                "opens": sorted(_opens),
                "mods": sorted(after - before),
                "cls": sorted(_cls),
                "ecls": sorted(_ecls),
            }
            with open(f"{COVDIR}/extra.{tag}.json", "w") as f:
                json.dump(out, f)
        except BaseException as e:  # noqa: BLE001
            sys.stderr.write(f"child error {item.nodeid}: {e!r}\n")
            code = 1
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    os.waitpid(pid, 0)
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
