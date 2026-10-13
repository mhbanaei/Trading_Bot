"""Start start.bat in a NEW visible console window (diagnostic helper).

``subprocess`` with ``CREATE_NEW_CONSOLE`` gives the bot its own CMD
window exactly like double-clicking start.bat does — no PowerShell/
``start`` quoting games. Used after a code change so the running bot
picks the new code up (the window survives this process exiting).

Usage:
    python scripts/dbg_launch_bot.py [path/to/start.bat]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREATE_NEW_CONSOLE = 0x00000010


def main() -> None:
    bat = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "start.bat"
    if not bat.exists():
        raise SystemExit("launcher not found: %s" % bat)
    proc = subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        cwd=str(bat.parent), creationflags=CREATE_NEW_CONSOLE)
    print("started pid %d: %s (cwd %s)" % (proc.pid, bat, bat.parent))


if __name__ == "__main__":
    main()
