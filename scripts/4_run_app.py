"""Convenience runner -- launches Streamlit via the Python module.

Equivalent to:
    streamlit run app/streamlit_app.py

Useful on Windows where the `streamlit` console script may not be on PATH.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    app = _REPO_ROOT / "app" / "streamlit_app.py"
    if not app.exists():
        print(f"App not found: {app}", file=sys.stderr)
        return 2
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), *sys.argv[1:]]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
