"""Hidden launcher for the US PIT forward-capture scheduled task.

Runs under pythonw.exe (no console window) and appends the capture result
to data/ops_logs/uspit_worker.log so failures remain diagnosable.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\Project\stock")
PYTHON = Path(r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe")
LOG_DIR = ROOT / "data" / "ops_logs"


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / "uspit_worker.log"
    completed = subprocess.run(
        [
            str(PYTHON),
            "-m",
            "research_platform",
            "us-pit",
            "capture-current",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            f"[{datetime.now().astimezone().isoformat()}] exit={completed.returncode}\n"
        )
        if completed.stdout.strip():
            handle.write(completed.stdout.strip() + "\n")
        if completed.stderr.strip():
            handle.write(completed.stderr.strip() + "\n")


if __name__ == "__main__":
    main()
