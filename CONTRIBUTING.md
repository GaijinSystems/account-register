# Contributing

Small fixes and bug reports are welcome. For anything larger, open an issue first so we can
agree on the approach. Security problems go through [SECURITY.md](SECURITY.md), not public
issues.

## Running the tests

The suite is plain `unittest` and needs Python 3.9 or later, `git` and Python's `cryptography` package:

```
python3 -m unittest discover -s tests
```

Or in a container, which needs nothing installed but podman (docker works the same way):

```
printf 'FROM debian:trixie\nRUN apt-get update && apt-get install -y --no-install-recommends python3 python3-cryptography git\n' \
    | podman build -t account-register-test -

podman run --rm --network=none -v "$PWD":/src:ro -w /src -e PYTHONDONTWRITEBYTECODE=1 \
    account-register-test python3 -m unittest discover -s tests
```

The tests write only to temporary directories, so the source can be mounted read-only. Run
one file with `-p test_web.py`. `pyflakes register tests tools build.py` should stay clean.

## Trying it with made-up data

```
python3 tools/demo_home.py /tmp/register-demo      # password: demo
python3 -m register run --home /tmp/register-demo  # http://127.0.0.1:8000
```

`tools/screenshots.py OUTDIR` retakes the README pictures from such a home and clicks through
the Security dialog in headless Chromium. It needs Playwright for Python, with Chromium, and
`cryptography`. Never take screenshots of real data.

The demo site (`demo/`, built by `tools/build_demo.py`) runs the real page against a
JavaScript port of the backend. After changing what an operation does or returns, change
`demo/api.js` to match and run `tools/demo_differential.py`, which compares the two.

`tools/a11y_audit.py PATH/TO/axe.min.js` runs the axe-core accessibility checks on every page
and dialog, in both themes and at phone width. It should report no violations; run it after
changing markup or colours.

## Ground rules

- **No new dependencies without discussion.** The app is the Python standard library plus
  `cryptography`, and ships as one file. The browser side is plain JavaScript with no build
  step and no outside resources.
- **The web process never imports `register.core`** and never touches the data directory. It
  can only send the recorder the operations listed in `register/protocol.py`. A new operation
  needs a matching `Ledger` method, validation in the recorder, and tests on both sides.
- **No operation may rewrite or delete git history,** and commit messages never contain row
  contents, amounts or names.
- **Amounts are decimal strings,** never floats. The balance is computed, never stored.
- **Every change comes with tests.** A bug fix starts with a test that fails without it.
- **Style:** follow the code around you. Lines up to 120 characters, standard library first,
  comments that say why, not what. Documentation is in plain, direct English.
- **README.md describes current behaviour;** update it in the same change, and add a line to
  `CHANGELOG.md`. `docs/DESIGN.md` explains how it is put together and why.

## License

By contributing you agree that your contribution is licensed under the
[GNU AGPL, version 3 or later](LICENSE), like the rest of the project.
