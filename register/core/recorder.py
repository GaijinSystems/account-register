"""The recorder process: sole owner of the data repository.

Listens on HOME/run/recorder.sock (0600, directory 0700) and accepts only the
operations listed in register.protocol.OPS. Every argument is validated here;
nothing from the web process is trusted. Requests are handled one at a time.

The recorder starts locked: only status, unlock and lock are accepted until a correct
password is given (the web login). It locks again on `lock` (logout), on a password change
(its own change_password op, or one made elsewhere), and after AUTO_LOCK_MINUTES without activity (status polls don't count).
"""
import contextlib
import json
import os
import signal
import socket
import socketserver
import struct
import sys
import time
import traceback

from .. import protocol
from ..throttle import Throttle
from . import crypto, formats
from .ledger import Ledger
from .store import StoreError, locked

AUTO_LOCK_MINUTES = 30
UNGATED = {"status", "unlock", "lock"}  # allowed while locked


class _Handler(socketserver.StreamRequestHandler):
    timeout = protocol.TIMEOUT

    def handle(self):
        if not _same_user(self.request):
            return
        line = self.rfile.readline(protocol.MAX_REQUEST + 1)
        if len(line) > protocol.MAX_REQUEST or not line.endswith(b"\n"):
            resp = _error("too_large", "request too large or incomplete")
        else:
            resp = self.server.dispatch(line)
        try:
            out = protocol.encode(resp)
        except ValueError:  # e.g. an error message quoting a path that is not valid Unicode
            out = protocol.encode(_error("internal", "internal error in the recorder"))
        with contextlib.suppress(OSError):  # the client went away; nothing to tell it
            self.wfile.write(out)


def _same_user(sock):
    """Reject connections from other users (SO_PEERCRED; Linux)."""
    if not hasattr(socket, "SO_PEERCRED"):
        return True
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid == os.getuid()


def _internal(op, e):
    """Log where an unexpected error happened and answer "internal". Never the exception's own
    text: it could quote a password, an amount or other row content."""
    tb = traceback.extract_tb(e.__traceback__)[-1]
    print("recorder: internal error in %s: %s at %s:%d" % (op, type(e).__name__, tb.filename, tb.lineno),
          file=sys.stderr, flush=True)
    return _error("internal", "internal error in the recorder")


def _error(code, message, field=None):
    out = {"ok": False, "error": code, "message": message}
    if field:
        out["field"] = field
    return out


class RecorderServer(socketserver.UnixStreamServer):
    def __init__(self, home, auto_lock_minutes=AUTO_LOCK_MINUTES, clock=time.monotonic):
        self.home = home
        self.ledger = Ledger(home)
        self.clock = clock
        self.auto_lock_seconds = auto_lock_minutes * 60
        self.last_activity = clock()
        self.throttle = Throttle(clock)
        self.kdf_n = crypto.DEFAULT_N  # scrypt cost for a changed password
        home.check()
        home.run.mkdir(mode=0o700, exist_ok=True)
        os.chmod(home.run, 0o700)
        path = str(home.socket_path)
        if os.path.exists(path):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(path)
                except OSError:
                    os.unlink(path)  # stale socket from a previous run
                else:
                    raise SystemExit("a recorder is already running on %s" % path)
        old = os.umask(0o177)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)

    def dispatch(self, line):
        try:
            op, args = protocol.check_request(json.loads(line.decode("utf-8")))
        except protocol.ProtocolError as e:
            return _error("bad_request", str(e), e.field)
        except (ValueError, RecursionError):
            return _error("bad_request", "request is not valid JSON")
        self.check_auto_lock()
        if op == "unlock":
            return self.unlock(args["password"])
        if op not in UNGATED and self.ledger.locked:
            return _error("locked", "the register is locked; log in to unlock it")
        if op == "export":
            return self.with_password(op, lambda: self.ledger.export(args["password"]))
        if op == "change_password":
            return self.with_password(op, lambda: self.ledger.change_password(n=self.kdf_n, **args))
        if op != "status":
            self.last_activity = self.clock()
        try:
            with locked(self.home, exclusive=op in protocol.WRITE_OPS):
                result = getattr(self.ledger, op)(**args)
            return {"ok": True, "result": result}
        except StoreError as e:
            if e.code == "locked":
                self.ledger.lock()
            return _error(e.code, e.message, e.field)
        except formats.FieldError as e:
            return _error(e.code, e.message, e.field)
        except Exception as e:  # never leak a traceback to the client
            return _internal(op, e)

    def unlock(self, password):
        wait = self.throttle.wait_seconds()
        if wait:
            return _error("throttled", "too many wrong passwords; try again in %d seconds" % wait)
        try:
            with locked(self.home, exclusive=False):
                result = self.ledger.unlock(password)
        except StoreError as e:
            if e.code == "bad_password":
                self.throttle.fail()
            return _error(e.code, e.message, e.field)
        except Exception as e:
            return _internal("unlock", e)
        self.throttle.success()
        self.last_activity = self.clock()
        return {"ok": True, "result": result}

    def with_password(self, op, run):
        """export and change_password ask for the password again; like unlock, a wrong one
        counts towards the same backoff. They take their own file locks."""
        wait = self.throttle.wait_seconds()
        if wait:
            return _error("throttled", "too many wrong passwords; try again in %d seconds" % wait)
        self.last_activity = self.clock()
        try:
            result = run()
        except StoreError as e:
            if e.code == "bad_password":
                self.throttle.fail()
            return _error(e.code, e.message, e.field)
        except Exception as e:
            return _internal(op, e)
        self.throttle.success()
        return {"ok": True, "result": result}

    def check_auto_lock(self):
        if not self.ledger.locked and self.clock() - self.last_activity > self.auto_lock_seconds:
            self.ledger.lock()
            print("recorder: locked after %d minutes without activity" % (self.auto_lock_seconds // 60),
                  flush=True)

    def service_actions(self):  # called by serve_forever between requests
        self.check_auto_lock()

    def server_close(self):
        super().server_close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(self.home.socket_path))


def _exit(signum, frame):
    raise SystemExit(0)


def serve(home, auto_lock_minutes=AUTO_LOCK_MINUTES):
    signal.signal(signal.SIGTERM, _exit)
    server = RecorderServer(home, auto_lock_minutes)
    print("recorder: listening on %s (locked until login)" % home.socket_path, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
