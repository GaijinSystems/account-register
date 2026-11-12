#!/usr/bin/env python3
"""Build dist/register.pyz: a single-file executable that runs with the system Python.

Uses only the standard library (zipapp). Usage: python3 build.py [--output PATH]
"""
import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(HERE / "register", Path(tmp) / "register",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy(HERE / "LICENSE", tmp)  # the file travels with its license
        zipapp.create_archive(tmp, target=output, interpreter="/usr/bin/env python3",
                              main="register.__main__:main", compressed=True)
    output.chmod(0o755)
    return output


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", default=str(HERE / "dist" / "register.pyz"))
    print("built", build(ap.parse_args().output))
