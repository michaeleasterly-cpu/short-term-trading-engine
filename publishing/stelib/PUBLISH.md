# PyPI publishing checklist — stelib

Operator-driven build + upload for the `stelib` package.
**Nothing here gets uploaded from CI** — this checklist runs from a
clean local shell, against the carved snapshot in this directory.

## What's in this directory

```
publishing/stelib/
├── pyproject.toml          # build config (setuptools backend)
├── LICENSE                 # Apache 2.0
├── PACKAGE_README.md       # the README that ships on PyPI
├── PUBLISH.md              # this file
└── stelib/                 # the carved Python package (59 modules)
```

The carve was generated automatically from `tpcore/` in the source
repo by the PR that introduced this directory. The source-of-truth
sanity checks at carve time were:

```bash
grep -rn "from tpcore\|import tpcore" publishing/stelib/stelib/    # empty
grep -rn "asyncpg\|psycopg\|alpaca\|anthropic" publishing/stelib/stelib/  # empty
```

If either of these now returns hits, the carve has drifted — fix
before publishing.

## Operator pre-flight checklist

1. **Re-run the sanity checks** from the repo root:
   ```bash
   grep -rn "from tpcore\|import tpcore" publishing/stelib/stelib/
   grep -rn "asyncpg\|psycopg\|alpaca\|anthropic" publishing/stelib/stelib/
   ```
   Both must return empty.

2. **Confirm the import surface** (in a clean venv with only the
   declared deps):
   ```bash
   python -m venv /tmp/stelib-smoke
   /tmp/stelib-smoke/bin/pip install pydantic numpy pandas scipy \
       exchange_calendars structlog python-dotenv
   PYTHONPATH=publishing/stelib /tmp/stelib-smoke/bin/python -c "
   import importlib, pkgutil, stelib
   for mi in pkgutil.walk_packages(stelib.__path__, prefix='stelib.'):
       importlib.import_module(mi.name)
   print('OK', stelib.__version__)
   "
   ```

3. **Fill in the project URLs** in `pyproject.toml`. The carve uses
   `<github-handle>/<repo>` as a placeholder — replace with the
   actual public-source URL before the first upload (PyPI rejects
   subsequent edits to the same version, so get this right on the
   first push).

4. **Install build + twine** into a clean venv (do NOT pollute the
   repo's working venv):
   ```bash
   python -m venv /tmp/stelib-build
   /tmp/stelib-build/bin/pip install --upgrade pip build twine
   ```

## Build

```bash
cd publishing/stelib
/tmp/stelib-build/bin/python -m build
```

Outputs go to `publishing/stelib/dist/` — a `.tar.gz` source dist and
a `.whl` wheel. The repo `.gitignore` has `publishing/**/dist/`
excluded so these are never committed.

## Twine validate

```bash
/tmp/stelib-build/bin/twine check dist/*
```

Must say `PASSED` for both artifacts before you upload anywhere.

## TestPyPI smoke (mandatory before real PyPI)

```bash
# First time only: register a TestPyPI account + an API token at
# https://test.pypi.org/manage/account/token/  and put it in
# ~/.pypirc or pass via env (`TWINE_USERNAME=__token__`,
# `TWINE_PASSWORD=pypi-…`).

/tmp/stelib-build/bin/twine upload --repository testpypi dist/*
```

Then in a third clean venv:

```bash
python -m venv /tmp/stelib-install
/tmp/stelib-install/bin/pip install \
    --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    stelib==0.1.0
/tmp/stelib-install/bin/python -c "import stelib; print(stelib.__version__)"
```

If anything fails, **bump the version**
(`pyproject.toml: version = "0.1.0"` → `"0.1.1"`) before re-uploading
— TestPyPI also rejects same-version re-uploads.

## Real PyPI upload

Only after TestPyPI is green:

```bash
/tmp/stelib-build/bin/twine upload dist/*
```

After the upload, the package is permanent at
`https://pypi.org/project/stelib/0.1.0/`. PyPI does not support
deletion of released versions — only "yank" (which hides from
default `pip install` but stays in history). Plan accordingly.

## After publish

- Tag the source commit: `git tag stelib-v0.1.0 && git push --tags`.
- Bump `pyproject.toml` to `0.1.1.dev0` (or the next planned
  version) so subsequent builds don't accidentally re-tag 0.1.0.
- Capture the published URL in the session log.

## Next version

Re-carve from the latest `tpcore/`:

1. Delete `publishing/stelib/stelib/` and the contents of `dist/`.
2. Re-run the carve scripts (see the original PR for the exact
   `cp -R` + `sed` invocations).
3. Re-run the two sanity-check `grep`s.
4. Bump the version in `pyproject.toml`.
5. Rebuild, re-test, re-upload.

Re-carve is preferable to incremental edits because the
tpcore→stelib rename + asyncpg type-hint scrub is mechanical and
easier to re-run than to maintain by hand.
