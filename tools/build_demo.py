#!/usr/bin/env python3
"""Build the static demo site: the app's own page, running against an in-memory stand-in for
the server (demo/api.js), with made-up data and no storage of any kind.

    python3 tools/build_demo.py OUTDIR [--cname DOMAIN]

OUTDIR then holds plain files with relative URLs only, for any static file host (the root of a
domain or a sub-path). --cname also writes the CNAME file GitHub Pages wants for a domain of
your own. Nothing in the repository is changed. See demo/README.md.
"""
import html
import json
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from register import __version__, protocol  # noqa: E402
from register.core import formats, ledger, money, recurring, store  # noqa: E402
from register.web import app  # noqa: E402

WEB = ROOT / "register" / "web"
DEMO = ROOT / "demo"
COPIED = tuple(app.STATIC)  # the app's own files (grid.js, grid.css, theme.js), byte for byte
# (file in demo/, name in the site): the scripts in the order they load, all before theme.js
SCRIPTS = (("nostore.js", "demo-nostore.js"), (None, "demo-spec.js"), ("api.js", "demo-api.js"))
STYLE = ("demo.css", "demo.css")

# The page may load its own files and nothing else, and may not connect anywhere: with
# connect-src 'none' the browser itself refuses fetch, XMLHttpRequest, WebSocket and beacons.
# (The real app sends its policy as a header; a static host cannot, so it is a meta element.
# frame-ancestors is not allowed there and is left out.)
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'none'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'")

# What the app's scripts must not start using without the demo being looked at again: every
# other way a page can keep data in the browser or talk to a server. (localStorage and fetch
# are the two it uses; demo/nostore.js and demo/api.js stand in for them.)
NOT_COVERED = re.compile(r"\b(sessionStorage|indexedDB|document\.cookie|cookieStore|caches\.|serviceWorker|"
                         r"openDatabase|XMLHttpRequest|sendBeacon|WebSocket|EventSource|importScripts|"
                         r"navigator\.storage|window\.name|RTCPeerConnection|navigator\.credentials|window\.open|"
                         r"(?:Shared)?Worker|BroadcastChannel|WebTransport)\b")


def js_regex(pattern):
    """A regular expression of the app's (anchored with \\Z, named groups) as JavaScript source."""
    body = pattern.removeprefix("^").removesuffix(r"\Z").replace("(?P<", "(?<")
    if "(?P" in body or r"\Z" in body or r"\A" in body:
        raise SystemExit("build_demo: cannot turn this pattern into JavaScript: %s" % pattern)
    return "^(?:%s)$" % body


def spec():
    """Everything demo/api.js takes from the app instead of repeating it: the routes and their
    arguments, the fields, the account types and the limits."""
    kinds = {str: "str", int: "int", bool: "bool", dict: "dict", list: "list", protocol.YEAR: "year"}
    return {
        "version": __version__,
        "routes": [{"method": method, "path": js_regex(pattern.pattern), "op": op,
                    "allowed": sorted(allowed), "required": sorted(required)}
                   for method, pattern, op, allowed, required in app.ROUTES],
        "ops": {op: {arg: [kinds[kind], required] for arg, (kind, required) in args.items()}
                for op, args in protocol.OPS.items()},
        "error_status": app.ERROR_STATUS,
        "fields": list(formats.FIELDS),
        "link_fields": list(formats.LINK_FIELDS),
        "search_fields": list(ledger.SEARCH_FIELDS),
        "text_limits": formats.TEXT_LIMITS,
        "name_limit": store.NAME_LIMIT,
        "min_year": formats.MIN_YEAR,
        "max_year": formats.MAX_YEAR,
        "max_abs": money.fmt(money.MAX_ABS),
        "transfer_category": formats.TRANSFER_CATEGORY,
        "opening": ledger.OPENING,
        "account_id": js_regex(store.ACCOUNT_ID.pattern),
        "account_id_limit": ledger.ID_LIMIT,
        "amount": js_regex(money._CANONICAL.pattern),
        "iso_date": js_regex(formats._ISO_DATE.pattern),
        "forbidden_categories": sorted(formats._FORBIDDEN_CATEGORIES),  # Unicode general categories
        "accounts_file": store.ACCOUNTS_FILE,
        "recurring_file": store.RECURRING_FILE,
        "link_id": js_regex(formats.LINK_ID.pattern),
        "types": [{"id": f.name, "label": f.label, "yearly": f.yearly, "owed_positive": f.owed_positive,
                   "debit_mode": f.debit_mode} for f in formats.FORMATS.values()],
        "recurring_every": {name: list(step) for name, step in recurring.EVERY.items()},
        "recurring_max_templates": recurring.MAX_TEMPLATES,
        "recurring_max_due": recurring.MAX_DUE,
        "max_suggestions": ledger.MAX_SUGGESTIONS,
        "max_results": ledger.MAX_RESULTS,
        "max_report_rows": ledger.MAX_REPORT_ROWS,
        "history_default": ledger.HISTORY_DEFAULT,
        "history_max": ledger.HISTORY_MAX,
        "history_changes": ledger.HISTORY_CHANGES,
    }


def swap(page, old, new):
    """Replace the one place `old` appears; anything else means the template has changed."""
    if page.count(old) != 1:
        raise SystemExit("build_demo: expected exactly one %r in index.html, found %d" % (old, page.count(old)))
    return page.replace(old, new)


def index_page():
    """The app's page as the server would send it, with its three files and the demo's own
    loaded by relative URL, the policy above, and the notice as the first thing in the body."""
    template = (WEB / "templates" / "index.html").read_text(encoding="utf-8")
    page = string.Template(template).substitute(csrf="demo", version=html.escape(__version__))
    head = ['<meta http-equiv="Content-Security-Policy" content="%s">' % html.escape(CSP)]
    head += ['<script src="%s"></script>' % name for _, name in SCRIPTS]
    head.append('<script src="theme.js"></script>')
    page = swap(page, '<script src="/static/theme.js"></script>', "\n".join(head))
    page = swap(page, '<link rel="stylesheet" href="/static/grid.css">',
                '<link rel="stylesheet" href="grid.css">\n<link rel="stylesheet" href="%s">' % STYLE[1])
    # no tag for grid.js: demo/api.js adds it once both stand-ins have loaded (fail closed)
    page = swap(page, '<script src="/static/grid.js" defer></script>', "")
    page = swap(page, '<body class="app">',
                '<body class="app">\n' + (DEMO / "notice.html").read_text(encoding="utf-8").strip())
    rooted = re.search(r"""(?:src|srcset|href|action|data|ping|poster)\s*=\s*["']?\s*(?:/|[a-z][a-z0-9+.-]*:)[^"'\s>]*""",
                       page, re.I)
    if rooted:
        raise SystemExit("build_demo: index.html still has a URL that is not relative: %s" % rooted.group())
    # a link or a refresh would take the visitor to a page the static host does not have
    if re.search(r"""<(?:a|area)\b[^>]*\bhref\s*=\s*["']?\s*[^#"'\s>]|http-equiv\s*=\s*["']?refresh""", page, re.I):
        raise SystemExit("build_demo: index.html has a link or a refresh that leaves the page")
    if re.search(r"<script(?![^>]*\bsrc=)|<style\b|\sstyle\s*=|\son[a-z]+\s*=", page, re.I):
        raise SystemExit("build_demo: index.html has an inline script, style or event handler")
    return page


def main(argv):
    cname = None
    if len(argv) == 4 and argv[2] == "--cname":
        argv, cname = argv[:2], argv[3]
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", cname):
            sys.exit("build_demo: --cname takes a host name like demo.example.org")
    if len(argv) != 2 or argv[1].startswith("-"):
        sys.exit(__doc__.strip())
    out = Path(argv[1])
    if out.exists() and not out.is_dir():
        sys.exit("build_demo: %s is not a directory" % out)
    files = {"index.html": index_page().encode("utf-8")}
    if cname:
        files["CNAME"] = (cname + "\n").encode("ascii")
    for name in COPIED:
        data = (WEB / "static" / name).read_bytes()
        if name.endswith(".js"):
            found = NOT_COVERED.search(data.decode("utf-8"))
            if found:
                raise SystemExit("build_demo: %s now uses %s, which the demo does not stand in for; see "
                                 "demo/README.md (\"Keeping it in step\")" % (name, found.group(1)))
        files[name] = data
    for source, name in SCRIPTS + (STYLE,):
        if source is None:
            text = ("// Written by tools/build_demo.py from the app's Python modules (version %s). Not edited by hand.\n"
                    "\"use strict\";\nwindow.REGISTER_DEMO_SPEC = %s;\n"
                    % (__version__, json.dumps(spec(), indent=1)))
            files[name] = text.encode("utf-8")
        else:
            files[name] = (DEMO / source).read_bytes()
    out.mkdir(parents=True, exist_ok=True)  # only now: every guard above has passed
    extra = sorted({p.name for p in out.iterdir()} - set(files))
    if extra:  # e.g. the CNAME of an earlier build: it would be published with the rest
        sys.exit("build_demo: %s holds other files (%s); use an empty directory" % (out, ", ".join(extra)))
    for name, data in files.items():
        (out / name).write_bytes(data)
    print("demo site written to %s:\n%s" % (out, "\n".join("  %-16s %7d bytes" % (n, len(d))
                                                         for n, d in sorted(files.items()))))


if __name__ == "__main__":
    main(sys.argv)
