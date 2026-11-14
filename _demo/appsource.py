"""Where the app's source is. The demo lives on the gh-pages branch; the app it shows is on main.
Set REGISTER_SOURCE to a checkout of the main branch (the directory holding register/)."""
import os
import sys
from pathlib import Path

_path = os.environ.get("REGISTER_SOURCE", "")
if not _path or not (Path(_path) / "register" / "__init__.py").is_file():
    sys.exit("set REGISTER_SOURCE to a checkout of account-register's main branch "
             "(the directory that holds register/); see _demo/README.md")
ROOT = Path(_path).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
