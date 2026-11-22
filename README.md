# Blast Radius entry

Selects, for each sqlglot patch, only the tests that can be affected by the change.

## How it works
- `map.json` is built once from the pinned sqlglot commit (`eb11aec`). It describes only that commit; there is no
  table from patches to tests.
  - Per-test line coverage: the whole suite is run with every test in its own forked process
    (`tools/covplugin.py`), so class setup and caches are never shared and each test's coverage is complete.
    Process pools are run inline so their work is traced too.
  - Import-time lines (`tools/import_cov.py`): lines that run while modules are imported or dialects are first
    created, and lines run while each test module is imported.
  - Which dialect/parser/generator classes and expression classes each test creates, which files it opens,
    function and statement spans, and an import map (`tools/build_map.py`).
- `src.zip` holds the pinned `sqlglot/` sources so the selector can read the original file at run time.
- `selector.py` parses each patch:
  - edits inside a function select the tests that run the changed statement (or the whole function for insertions);
  - edits to code that runs at import time select every test that uses that module or dialect family;
  - comment-only and whitespace-only edits select nothing that can change;
  - fixture edits select the tests that open the fixture; test-file edits select that file and its importers;
  - anything it cannot reason about (syntax errors, core module-level edits, config, deleted or renamed files)
    runs every test.
- Tests are packed onto at most 8 workers, choosing the worker count that minimises `max load + 2 s * workers`.

## Rebuilding the map
Clone sqlglot at the pinned commit, `pip install -r requirements.lock coverage`, then with `SKIP_INTEGRATION=0`:
1. `pytest tests -p covplugin -n 2` with `tools/` on `PYTHONPATH` (writes per-test data to `COV_DIR`);
2. `python tools/import_cov.py tests.json`;
3. `python tools/build_map.py`.
