"""account-register command line.

    register.pyz init            --home DIR       create a new (encrypted) register home
    register.pyz change-password --home DIR       re-encrypt everything under a new password
    register.pyz add-account     --home DIR ...   add an account (same as the web dialog)
    register.pyz import          --home DIR FILE  a spreadsheet (.ods, .xlsx, .csv) or a config;
                                                  shows a preview and asks before writing
    register.pyz import-template FILE [--output CONFIG.json]   the guessed config, to edit
    register.pyz export          --home DIR OUTDIR   every sheet as CSV + a config to re-import it
    register.pyz show            --home DIR PATH [--rev COMMIT]   decrypted file
    register.pyz history         --home DIR PATH [-n N]           decrypted changes
    register.pyz textconv        --home DIR FILE  git diff driver (uses REGISTER_PASSWORD)
    register.pyz recorder        --home DIR       run the recorder process
    register.pyz serve           --home DIR       run the web process
    register.pyz run             --home DIR       run both (two child processes)

Commands that read or write data ask for the password once (or use REGISTER_PASSWORD).
"""
import argparse
import getpass
import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ACCOUNT_TYPES = ["checking", "savings", "card", "cash", "loan"]  # formats.FORMATS (not imported here: it is lazy)
DEFAULT_CONFIG = {
    "bind": "127.0.0.1",
    "port": 8000,
    "cookie_secure": True,
    "public_origin": None,
    "session_idle_minutes": 30,
    "session_max_hours": 12,
    "auto_lock_minutes": 30,
}


def _ask(prompt="Password: "):
    """REGISTER_PASSWORD if set, otherwise a terminal prompt."""
    return os.environ.get("REGISTER_PASSWORD") or getpass.getpass(prompt)


def _new_password():
    if os.environ.get("REGISTER_PASSWORD") and not sys.stdin.isatty():
        return os.environ["REGISTER_PASSWORD"]  # scripted setup
    pw = getpass.getpass("New password: ")
    if not pw:
        sys.exit("the password cannot be empty")
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("passwords do not match")
    return pw


def _unlock(home):
    """Prove the password for a command: returns the key or exits."""
    from .core.store import StoreError
    try:
        home.check()
        return home.unlock(_ask())
    except StoreError as e:
        sys.exit("error: %s" % e.message)


def _home(args):
    from .core.store import Home
    return Home(args.home)


def write_config(path, cfg):
    tmp = path.with_name(".tmp-config.json")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return {**DEFAULT_CONFIG, **cfg}


def cmd_init(args):
    from .core import admin
    from .core.store import StoreError
    home = _home(args)
    if admin.is_initialized(home):
        sys.exit("%s is already initialized" % home.root)
    print("Choose the password. It encrypts all data; if it is lost, the data cannot be recovered.")
    pw = _new_password()
    try:
        admin.init_home(home, pw, args.git_name, args.git_email)
    except StoreError as e:
        sys.exit("error: %s" % e.message)
    if not home.config.exists():
        write_config(home.config, DEFAULT_CONFIG)
    print("initialized %s" % home.root)


def cmd_change_password(args):
    from .core import admin
    from .core.store import StoreError
    home = _home(args)
    old = getpass.getpass("Current password: ")
    try:
        home.check()
        home.unlock(old)
    except StoreError as e:
        sys.exit("error: %s" % e.message)
    new = _new_password()
    try:
        n = admin.change_password(home, old, new)
    except StoreError as e:
        sys.exit("error: %s" % e.message)
    print("password changed; %d sheet(s) and the account list were re-encrypted in one commit.\n"
          "A running app locks itself and needs the new password at the next login." % n)


def cmd_add_account(args):
    from .core import money
    from .core.ledger import Ledger
    from .core.store import StoreError, locked
    home = _home(args)
    try:
        opening = money.parse_loose(args.opening or "")
        opening = "" if opening is None else money.fmt(opening)
    except ValueError as e:
        sys.exit("error: opening balance: %s" % e)
    key = _unlock(home)
    try:
        with locked(home):
            res = Ledger(home, key).add_account(args.name, args.type, opening, args.year, account_id=args.id)
    except StoreError as e:
        sys.exit("error: %s" % e.message)
    print("added account %s" % res["account"])


DIRECT_OPTIONS = ("sheet", "account", "name", "type", "year", "date_format", "save_config")


def cmd_import(args):
    """Import a spreadsheet directly (the mapping is guessed), or what a config describes. Either
    way: a preview first, then a question (or --yes), then one commit."""
    import copy
    from .core import formats, importer, template
    from .core.ledger import slugify
    from .core.store import Store, StoreError
    home = _home(args)
    path = Path(args.file)
    from .core import bankfile
    if path.is_file() and bankfile.sniff(path):
        return _import_bank_file(args, home, path)
    direct = path.is_file() and not importer.is_config(path)
    if args.append and not direct:
        sys.exit("error: --append is for a spreadsheet, CSV or bank file, not for a config")
    for option in ("replace", "balance_check"):
        if args.append and getattr(args, option):
            sys.exit("error: --%s does not apply with --append: rows are added, nothing is replaced or compared "
                     "with the file's balances" % option.replace("_", "-"))
    save = Path(args.save_config) if args.save_config else None
    try:  # everything that can be checked without the password, before asking for it
        if direct:
            if args.source:
                sys.exit("error: --source is for configs; %s is already the file to import" % path)
            if save and save.exists() and not args.force:
                sys.exit("error: %s already exists (use --force to overwrite)" % save)
            result = template.guess(path, save.parent if save else None, need_balance=not args.append)
        else:
            used = [o for o in DIRECT_OPTIONS if getattr(args, o)]
            if used:
                sys.exit("error: --%s are for importing a spreadsheet directly; with a config, edit the config"
                         % ", --".join(o.replace("_", "-") for o in used))
            cfg = importer.load_config(path)
            if args.source:
                _replace_source(cfg, Path(args.source))
    except (importer.ImportFailed, template.TemplateError) as e:
        sys.exit("error: %s" % e)
    key = _unlock(home)
    try:
        if direct:
            existing = {a["id"]: a for a in Store(home, key).accounts()}
            account = args.account or (_only_account(args, existing) if args.append else None) \
                or (slugify(args.name) if args.append and args.name else None)  # as for a bank file
            if args.append:
                if not (account or args.name or args.type) and existing:
                    sys.exit("error: say which account the rows go to with --account ID (existing: %s), or name "
                             "a new one with --name" % ", ".join(existing))
            chosen = template.choose(result, existing, args.sheet, account, args.name, args.type, args.year,
                                     args.date_format)
            print("How %s was read (guessed; check the preview below):" % path.name)
            for line in result.report + chosen:
                print("  " + line)
            if save:
                save.write_text(template.dumps_config(result.config), encoding="utf-8")
                print("saved the mapping to %s; to import the next file of the same layout:\n"
                      "  register.pyz import --home %s %s --source NEWFILE" % (save, home.root, save))
            cfg = importer.check_config(copy.deepcopy(result.config), save.parent if save else Path.cwd())
            print()
        if args.append:
            target, rows, notes = importer.rows_of_config(home, cfg, key)
            if isinstance(target, str) and (args.type or args.name):
                sys.exit("error: account %s already exists; --name and --type only describe a new account" % target)
            return _confirm_append(args, home, key, target, rows, path, notes)
        print("Preview (nothing has been written yet):\n")
        preview = importer.run_import(home, cfg, key, dry_run=True, replace=args.replace,
                                      balance_check=args.balance_check,
                                      out=print if args.dry_run else _all_but_dry_run_note)
        if args.dry_run:
            return
        if not _confirmed(args, "Import this into %s?" % home.root):
            return
        importer.run_import(home, cfg, key, replace=args.replace, balance_check=args.balance_check,
                            expect=preview)
    except (importer.ImportFailed, template.TemplateError, StoreError, formats.FieldError) as e:
        sys.stdout.flush()
        sys.exit("error: %s" % (e.message if isinstance(e, StoreError) else e))


def _only_account(args, ids):
    """Appending without --account: the only existing account, unless a new one is described."""
    return next(iter(ids)) if len(ids) == 1 and not args.name and not args.type else None


def _confirmed(args, question):
    """The question after a preview: True to go ahead (--yes answers it)."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        sys.exit("not imported: there is no terminal to ask on; run again with --yes to import this")
    try:
        answer = input(question + " [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    print("nothing was written")
    return False


def _import_bank_file(args, home, path):
    """An OFX, QFX or QIF download: its transactions are appended to one account."""
    from .core import bankfile, formats, importer, template
    from .core.ledger import slugify
    from .core.store import ACCOUNT_ID, NAME_LIMIT, Store, StoreError, new_account
    if args.sheet and len(args.sheet) > 1:
        sys.exit("error: a bank file is imported one account at a time; give --sheet once")
    for option in ("save_config", "source", "year", "replace", "balance_check"):
        if getattr(args, option):
            sys.exit("error: --%s does not apply to a bank file; its rows are added to the end of an account"
                     % option.replace("_", "-"))
    try:
        date_format = template.date_format_option(args.date_format) if args.date_format else None
        statement = bankfile.read(path, args.sheet[0] if args.sheet else None, date_format)
    except (bankfile.BankFileError, template.TemplateError) as e:
        sys.exit("error: %s" % e)
    key = _unlock(home)
    try:
        accounts = Store(home, key).accounts()
        target = args.account or _only_account(args, [a["id"] for a in accounts]) \
            or (slugify(args.name) if args.name else None)
        if target is None:
            sys.exit("error: say which account the rows go to with --account ID (existing: %s), or name a "
                     "new one with --name" % (", ".join(a["id"] for a in accounts) or "none"))
        if not any(a["id"] == target for a in accounts):
            if not ACCOUNT_ID.match(target):
                sys.exit("error: account id %r: use lower-case letters, digits and hyphens" % target)
            name = formats.check_text((args.name or target.replace("-", " ").capitalize()).strip(), "name", NAME_LIMIT)
            if not name:
                sys.exit("error: the account name cannot be empty")
            account_id, target = target, new_account(target, name, args.type or statement.account_type)
            account_type = target["type"]
        elif args.name or args.type:
            sys.exit("error: account %s already exists; --name and --type only describe a new account" % target)
        else:
            account_id, account_type = target, next(a["type"] for a in accounts if a["id"] == target)
        notes = list(statement.notes)
        if (account_type == "card") != (statement.account_type == "card"):
            notes.append("WARNING: the file is a %s statement, but %s is a %s account. CHECK that this is "
                         "the right account" % ("credit card" if statement.account_type == "card" else "bank",
                                                account_id, account_type))
        _confirm_append(args, home, key, target, statement.rows, path, notes)
    except (importer.ImportFailed, StoreError, formats.FieldError) as e:
        sys.stdout.flush()
        sys.exit("error: %s" % (e.message if isinstance(e, StoreError) else e))


def _confirm_append(args, home, key, account, rows, source, notes):
    """Preview, question (or --yes), then the append; as for a full import."""
    from .core import importer
    print("Preview (nothing has been written yet):\n")
    preview = importer.run_append(home, key, account, rows, source, dry_run=True, notes=notes,
                                  out=print if args.dry_run else _all_but_dry_run_note)
    if args.dry_run:
        return
    if not preview.changed:  # nothing to ask, and nothing to run again
        print(importer.NOTHING_NEW if rows else importer.NO_ROWS)
    elif _confirmed(args, "Add these rows to %s?" % home.root):
        importer.run_append(home, key, account, rows, source, expect=preview, notes=notes)


def _all_but_dry_run_note(line):
    """The preview before the question: the closing "dry run: nothing was written" would only
    confuse."""
    if not line.startswith("dry run:"):
        print(line)


def _replace_source(cfg, source):
    """--source: read another file of the same layout instead of the one the config names."""
    from .core import importer
    if cfg.get("exact"):
        raise importer.ImportFailed("--source does not apply to an export's config")
    named = {e["source"] for e in cfg["sheets"]}
    if len(named) != 1:
        raise importer.ImportFailed("--source needs a config that reads a single file (this one reads %s)"
                                    % ", ".join(sorted(named)))
    if not source.is_file():
        raise importer.ImportFailed("%s does not exist" % source)
    for e in cfg["sheets"]:
        e["source"] = source.name
        e["source_path"] = source.resolve()


def cmd_export(args):
    from .core import export
    from .core.store import StoreError
    home = _home(args)
    try:
        export.check_outdir(home, args.outdir)  # fail fast, before the password prompt
    except export.ExportError as e:
        sys.exit("error: %s" % e)
    key = _unlock(home)
    try:
        out, accounts, sheets, rows = export.export(home, key, args.outdir, args.format, args.currency)
    except (export.ExportError, StoreError) as e:
        sys.exit("error: %s" % (e.message if isinstance(e, StoreError) else e))
    restore = "restore with: register.pyz import --home NEWHOME %s" % (out / export.CONFIG_NAME) \
        if args.format == "csv" else \
        "This format is for other programs; only the default CSV export restores a register exactly."
    print("exported %d account(s), %d sheet(s), %d row(s) to %s\n%s\n"
          "These files are your data, decrypted. Delete them once they are no longer needed."
          % (accounts, sheets, rows, out, restore))


def cmd_show(args):
    from .core import admin
    from .core.store import StoreError
    try:
        for line in admin.show(_home(args), args.path, args.rev, _ask):
            print(line)
    except StoreError as e:
        sys.exit("error: %s" % e.message)


def cmd_history(args):
    from .core import admin
    from .core.store import StoreError
    try:
        entries = admin.history(_home(args), args.path, args.n, _ask)
    except StoreError as e:
        sys.exit("error: %s" % e.message)
    for commit, date, subject, diff in entries:
        print("%s  %s  %s" % (commit[:10], date, subject))
        for line in diff:
            if not line.startswith("@@"):
                print("    " + line)
        print()


def cmd_textconv(args):
    from .core import admin
    from .core.store import StoreError
    try:
        lines = admin.textconv(_home(args), args.file, os.environ.get("REGISTER_PASSWORD"))
    except StoreError as e:
        lines = ["[account-register: %s]" % e.message]
    sys.stdout.write("".join(line + "\n" for line in lines))


def cmd_import_template(args):
    from .core import template
    out = Path(args.output) if args.output else None
    if out and out.exists() and not args.force:
        sys.exit("error: %s already exists (use --force to overwrite)" % out)
    try:
        config, report = template.build_template(args.source, out.parent if out else None)
    except template.TemplateError as e:
        sys.exit("error: %s" % e)
    text = template.dumps_config(config)
    if out:
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print("\n".join(["guesses (review the config, then run import --dry-run):"] +
                    ["  " + line for line in report]), file=sys.stderr)
    if out:
        print("wrote %s" % out, file=sys.stderr)


def cmd_recorder(args):
    from .core import recorder
    home = _home(args)
    try:
        home.check()
    except Exception as e:
        sys.exit(str(e))
    minutes = read_config(home.config)["auto_lock_minutes"] if home.config.exists() else DEFAULT_CONFIG["auto_lock_minutes"]
    recorder.serve(home, minutes)


def cmd_serve(args):
    from .web import server
    home = _home(args)
    if not home.config.exists():
        sys.exit("no config.json in %s (run init first)" % home.root)
    server.serve(read_config(home.config), home.socket_path)


def _launcher():
    """Command prefix that runs this program again in a fresh interpreter."""
    if zipfile.is_zipfile(sys.argv[0]):
        return [sys.executable, os.path.abspath(sys.argv[0])], None
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=pkg_parent + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return [sys.executable, "-m", "register"], env


def cmd_run(args):
    home = _home(args)
    prefix, env = _launcher()
    procs = []

    def stop(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()

    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))
    try:
        procs.append(subprocess.Popen(prefix + ["recorder", "--home", str(home.root)], env=env))
        for _ in range(100):
            if home.socket_path.exists() or procs[0].poll() is not None:
                break
            time.sleep(0.05)
        if procs[0].poll() is not None:
            sys.exit("recorder failed to start")
        procs.append(subprocess.Popen(prefix + ["serve", "--home", str(home.root)], env=env))
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        failed = [p for p in procs if p.poll() not in (None, 0)]
        stop()
        sys.exit(1 if failed else 0)
    except KeyboardInterrupt:
        stop()


QUICKSTART = """\
getting started (first time, from nothing to live):
  1. register.pyz init --home ~/register
       choose the password: it encrypts all data and cannot be recovered if lost
  2. register.pyz import --home ~/register workbook.ods     (once per file; none? skip this)
       .ods, .xlsx or .csv; it shows how the file was read and a preview, then asks.
       If a guess is wrong: --account/--type/--year/--sheet, or --save-config to edit
  3. edit ~/register/config.json: bind and port; cookie_secure true behind HTTPS;
       public_origin if your reverse proxy rewrites the Host header
  4. register.pyz run --home ~/register
       or run "recorder" and "serve" as two systemd units (see README)
  5. open the site and log in with the password: logging in unlocks the data

later: change-password, add-account, show / history (decrypted), import --replace,
       export (every sheet as CSV, with a config that imports it back exactly)
every command has its own help: register.pyz COMMAND --help
"""


# Everything that reads, writes or unlocks data needs the cryptography package.
NEEDS_CRYPTO = {"init", "change-password", "add-account", "import", "export", "show", "history", "textconv",
                "recorder"}
MISSING_CRYPTO = ("Python's 'cryptography' package is not installed. It is required to encrypt and read\n"
                  "the data. Install it with: apt install python3-cryptography   (Debian/Ubuntu)")


def _crypto_missing():
    from .core import crypto
    return crypto.AESGCM is None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    missing = _crypto_missing()
    epilog = QUICKSTART + ("\nWARNING: " + MISSING_CRYPTO + "\n" if missing else "")
    ap = argparse.ArgumentParser(prog="register.pyz", description="minimal git-backed check register",
                                 epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    ap.add_argument("--version", action="version", version="account-register " + __version__)
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", title="commands")

    def add(name, func, help):
        p = sub.add_parser(name, help=help)
        p.add_argument("--home", required=True, help="register home directory")
        p.set_defaults(func=func)
        return p

    p = add("init", cmd_init, "create a new register home")
    p.add_argument("--git-name", default="account-register", help="commit author name")
    p.add_argument("--git-email", default="account-register@localhost", help="commit author email")
    add("change-password", cmd_change_password, "re-encrypt all data under a new password")
    p = add("add-account", cmd_add_account, "add an account")
    p.add_argument("name")
    p.add_argument("--type", required=True, choices=ACCOUNT_TYPES,
                   help="card is a credit card; checking and card keep one tab per year")
    p.add_argument("--opening", help="opening balance, e.g. 1234.56; negative for what is owed on a card or loan")
    p.add_argument("--year", type=int, help="first year (checking and card accounts)")
    p.add_argument("--id", help="account id (default: made from the name)")
    p = add("import", cmd_import, "import a spreadsheet, CSV or bank file (or what an import config describes)")
    p.add_argument("file", help=".ods, .xlsx or .csv file (Google Sheets: download as one of those), a bank "
                                "download (.ofx, .qfx, .qif), or an import config (.json)")
    p.add_argument("--dry-run", action="store_true", help="show the preview only; write nothing")
    p.add_argument("--yes", "-y", action="store_true", help="import after the preview without asking")
    p.add_argument("--replace", action="store_true", help="overwrite sheets that already exist")
    p.add_argument("--append", action="store_true",
                   help="add the file's rows to the end of the account, skipping the ones already there "
                        "(bank files always do this)")
    p.add_argument("--balance-check", choices=["strict", "warn", "off"],
                   help="whether running balances that differ from the file stop the import (default strict)")
    g = p.add_argument_group("importing a spreadsheet directly")
    g.add_argument("--sheet", action="append", help="import only this sheet of a workbook (repeatable)")
    g.add_argument("--account", help="the account to import into: an existing id, or a new one")
    g.add_argument("--name", help="display name for a new account")
    g.add_argument("--type", choices=ACCOUNT_TYPES, help="type for a new account")
    g.add_argument("--year", type=int, help="the year of the sheet, for a checking account")
    g.add_argument("--date-format", help="how dates written as text read, e.g. DD/MM/YYYY (when the guess is wrong)")
    g.add_argument("--save-config", metavar="CONFIG.json", help="also save the mapping, to edit or reuse")
    g.add_argument("--force", action="store_true", help="overwrite --save-config if it exists")
    g = p.add_argument_group("importing with a config")
    g.add_argument("--source", metavar="FILE", help="read this file instead of the one the config names "
                                                    "(a later file with the same layout)")
    p = add("export", cmd_export, "write every sheet as CSV, plus a config that imports it back exactly")
    p.add_argument("outdir", help="a new or empty directory outside the register home")
    p.add_argument("--format", choices=["csv", "json", "qif", "ofx"], default="csv",
                   help="csv (default) restores exactly; json is one file with everything; qif and ofx are "
                        "one file per account, for other programs")
    p.add_argument("--currency", default="USD", help="currency code written into ofx files (default USD)")
    p = sub.add_parser("import-template", help="write the guessed import config for a file, to edit")
    p.add_argument("source", help=".ods, .xlsx or .csv file")
    p.add_argument("--output", help="write the config here (default: print it)")
    p.add_argument("--force", action="store_true", help="overwrite --output if it exists")
    p.set_defaults(func=cmd_import_template)
    p = add("show", cmd_show, "print a decrypted data file (now or at a commit)")
    p.add_argument("path", help="e.g. checking/2026.jsonl or accounts.json")
    p.add_argument("--rev", help="git commit (default: the current file)")
    p = add("history", cmd_history, "show decrypted changes to a data file, newest first")
    p.add_argument("path", help="e.g. checking/2026.jsonl")
    p.add_argument("-n", type=int, default=20, help="number of commits (default 20)")
    p = add("textconv", cmd_textconv, "git diff driver: decrypt FILE using REGISTER_PASSWORD")
    p.add_argument("file")
    add("recorder", cmd_recorder, "run the recorder process")
    add("serve", cmd_serve, "run the web process")
    add("run", cmd_run, "run the recorder and web processes together")
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return
    if missing and args.cmd in NEEDS_CRYPTO:
        sys.exit("error: " + MISSING_CRYPTO)  # before any password prompt or file changes
    from .core.crypto import CryptoError
    try:
        args.func(args)
    except CryptoError as e:
        sys.exit("error: " + e.message)


if __name__ == "__main__":
    main()
