"""Shared by the browser tools: the app running on a fresh demo home (tools/demo_home.py)."""
import contextlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8000"
PASSWORD = "demo"


@contextlib.contextmanager
def running(quiet=False):
    """Start the app on a new demo home; stop it and delete the home afterwards."""
    sink = subprocess.DEVNULL if quiet else None
    try:  # never drive some other instance: nothing may be on the port before the demo starts
        socket.create_connection(("127.0.0.1", 8000), timeout=1).close()
    except OSError:
        pass
    else:
        raise SystemExit("something is already listening on port 8000; stop it first")
    with tempfile.TemporaryDirectory() as tmp:
        home = str(Path(tmp) / "demo")
        subprocess.run([sys.executable, str(ROOT / "tools" / "demo_home.py"), home], check=True, stdout=sink)
        app = subprocess.Popen([sys.executable, "-m", "register", "run", "--home", home], cwd=ROOT,
                               stdout=sink, stderr=sink)
        try:
            for _ in range(100):
                if app.poll() is not None:  # e.g. port 8000 is taken: never talk to some other instance
                    raise SystemExit("the demo app did not start (is something else on port 8000?)")
                try:
                    urllib.request.urlopen(URL + "/login").close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise SystemExit("the demo app did not answer on %s" % URL)
            yield
        finally:
            app.terminate()
            app.wait()


def login(page, password=PASSWORD):
    page.goto(URL + "/login")
    page.fill("input[name=password]", password)
    page.click("button[type=submit]")
    page.wait_for_selector("#grid tr")
