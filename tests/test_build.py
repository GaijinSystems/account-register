"""Build register.pyz and exercise it as a standalone executable."""
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build  # noqa: E402
import register  # noqa: E402
from helpers import checking_rows, write_ods  # noqa: E402


class BuildTest(unittest.TestCase):
    def test_pyz_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pyz = build.build(tmp / "register.pyz")
            home = tmp / "home"

            env = {"PATH": "/usr/bin:/bin", "REGISTER_PASSWORD": "build test password"}

            def run(*args):
                return subprocess.run([str(pyz), *args], capture_output=True, text=True, timeout=60,
                                      stdin=subprocess.DEVNULL, env=env)

            for args in ((), ("--help",)):
                r = run(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("getting started", r.stdout)
                self.assertIn("1. register.pyz init", r.stdout)
            r = run("--version")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "account-register " + register.__version__))
            with zipfile.ZipFile(pyz) as z:
                self.assertIn("GNU AFFERO GENERAL PUBLIC LICENSE", z.read("LICENSE").decode())
            r = run("init", "--home", str(home))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((home / "config.json").stat().st_mode & 0o777, 0o600)
            write_ods(tmp / "wb.ods", {"2025": checking_rows("10.00", [
                (None, "2025-01-03", "Gas", "4.00", "x", None, None, None)])})
            (tmp / "imp.json").write_text(json.dumps({
                "accounts": [{"id": "checking", "name": "Checking", "type": "checking"}],
                "sheets": [{"source": "wb.ods", "sheet": "2025", "account": "checking", "year": 2025,
                            "start_row": 3, "opening": "H1",
                            "columns": {"date": "B", "description": "C", "debit": "D", "cleared": "E",
                                        "credit": "F", "balance": "H"}}]}))
            r = run("import", "--home", str(home), str(tmp / "imp.json"))
            self.assertNotEqual(r.returncode, 0)  # no terminal to ask on
            self.assertIn("--yes", r.stderr)
            r = run("import", "--home", str(home), str(tmp / "imp.json"), "--yes")
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertIn("imported and committed", r.stdout)
            raw = (home / "data" / "checking" / "2025.jsonl").read_text()
            self.assertNotIn("Gas", raw)  # encrypted at rest
            r = run("show", "--home", str(home), "checking/2025.jsonl")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout.splitlines()[1])["amount"], "-4.00")
            r = run("add-account", "--home", str(home), "Money Market", "--type", "savings", "--opening", "$1,000")
            self.assertEqual(r.returncode, 0, r.stderr)
            env["REGISTER_PASSWORD"] = "wrong"
            r = run("show", "--home", str(home), "accounts.json")
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
