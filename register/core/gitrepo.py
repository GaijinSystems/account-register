"""Append-only git wrapper for the data repository.

Deliberately limited to: init, status, add, commit, plus read-only log and show
(for the history commands and the change log; an undo is a new commit like any other edit). There is no reset, checkout, restore, amend, rebase,
push or branch code in this application.
git is always run with an argument list (never a shell), with hooks and
commit signing disabled and no interactive prompts.
"""
import os
import subprocess

_BASE = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false"]
_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


class GitError(RuntimeError):
    pass


def _run(repo, *args, binary=False):
    env = dict(os.environ, **_ENV)
    proc = subprocess.run(_BASE + ["-C", str(repo)] + list(args), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
                          **({} if binary else {"encoding": "utf-8", "errors": "replace"}))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace") if binary else proc.stderr
        raise GitError("git %s failed: %s" % (args[0], err.strip()))
    return proc.stdout


def init(repo, name, email):
    _run(repo, "init", "--quiet")
    _run(repo, "config", "user.name", name)
    _run(repo, "config", "user.email", email)


def is_clean(repo):
    """True when the working tree and index match HEAD (untracked files count as dirty)."""
    return _run(repo, "status", "--porcelain", "--untracked-files=all") == ""


def add(repo, paths):
    """Stage the given paths, including deletions of any that no longer exist."""
    _run(repo, "add", "--all", "--", *paths)


def commit(repo, message):
    _run(repo, "commit", "--quiet", "--no-verify", "-m", message)


def log(repo, path, limit):
    """Commits touching path, newest first: [(hash, "YYYY-MM-DD HH:MM", subject)]. Read-only."""
    out = _run(repo, "log", "-n", str(int(limit)), "--format=%H%x09%ad%x09%s",
               "--date=format:%Y-%m-%d %H:%M", "--", path)
    return [tuple(line.split("\t", 2)) for line in out.splitlines() if line]


def log_with_files(repo, path, limit):
    """As log, with the time in full (ISO 8601, with its offset) and every path each commit
    touched: [(hash, time, subject, [paths])]. One git call for the whole list. Read-only."""
    out = _run(repo, "log", "-n", str(int(limit)), "--format=%x01%H%x09%ad%x09%s", "--date=iso-strict",
               "--name-only", "--full-diff", "--", path)
    commits = []
    for record in out.split("\x01")[1:]:
        head, *names = record.split("\n")  # not splitlines(): a subject may hold other line separators
        parts = head.split("\t", 2)
        if len(parts) == 3:
            commits.append((*parts, [n for n in names if n]))
    return commits


def show(repo, rev, path):
    """File content at a revision (bytes), or None if it did not exist there. Read-only."""
    try:
        return _run(repo, "show", "--end-of-options", "%s:%s" % (rev, path), binary=True)
    except GitError:
        return None


def last_change(repo, path):
    """ISO 8601 time (with offset) of the last commit touching path, or None. Read-only."""
    out = _run(repo, "log", "-1", "--format=%cI", "--", path).strip()
    return out or None
