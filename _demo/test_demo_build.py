"""The static demo (_demo/build_demo.py) is built from the app's own page and modules. Building
it here makes its guards fire when the app changes in a way the demo does not cover: a new
static file or template variable, a storage or network call in the page's scripts, a protocol
type the generated spec cannot express.

    REGISTER_SOURCE=PATH/TO/main python3 -m unittest discover -s _demo"""
import contextlib
import html
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_demo  # noqa: E402
from appsource import ROOT  # noqa: E402


class DemoBuildTest(unittest.TestCase):
    def test_the_demo_builds_from_the_current_app(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            build_demo.main(["build_demo", tmp, "--cname", "demo.example.org"])
            files = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
            with self.assertRaises(SystemExit):   # an earlier build's CNAME would be published too
                build_demo.main(["build_demo", tmp])
        self.assertEqual(sorted(files), ["CNAME", "demo-api.js", "demo-nostore.js", "demo-spec.js", "demo.css",
                                         "grid.css", "grid.js", "index.html", "theme.js"])
        self.assertEqual(files["CNAME"], b"demo.example.org\n")
        for name in ("grid.js", "grid.css", "theme.js"):     # the app's own files, unchanged
            self.assertEqual(files[name], (ROOT / "register" / "web" / "static" / name).read_bytes())
        page = files["index.html"].decode("utf-8")
        self.assertNotIn("/static/", page)
        self.assertNotIn("$", page.split("<body")[0])        # no template placeholder left in the head
        self.assertIn("connect-src 'none'", html.unescape(page))   # the page may not connect anywhere
        self.assertLess(page.index("demo-nostore.js"), page.index("demo-api.js"))   # the stand-ins, in order
        self.assertNotIn('src="grid.js"', page)   # demo-api.js starts it, and only with both stand-ins in place
        self.assertIn(b"REGISTER_DEMO_NOSTORE", files["demo-nostore.js"])
        self.assertIn(b"REGISTER_DEMO_NOSTORE", files["demo-api.js"])
        spec = json.loads(files["demo-spec.js"].decode("utf-8").split("window.REGISTER_DEMO_SPEC = ", 1)[1].rstrip(";\n"))
        from register import protocol
        self.assertEqual(set(spec["ops"]), set(protocol.OPS))


if __name__ == "__main__":
    unittest.main()
