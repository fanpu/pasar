"""Entry point systemd runs for each attempt: exec the job's command with its environment."""

import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    job_dir = Path((argv if argv is not None else sys.argv[1:])[0])
    spec = json.loads((job_dir / "launch.json").read_text())
    os.chdir(spec["cwd"])
    os.execve("/bin/bash", ["bash", "-c", spec["command"]], spec["env"])


if __name__ == "__main__":
    main()
