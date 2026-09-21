import json
import subprocess
import sys


def test_launch_execs_command_with_env_and_cwd(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "launch.json").write_text(json.dumps({
        "command": 'echo "$FOO"; pwd',
        "cwd": str(work),
        "env": {"FOO": "bar", "PATH": "/usr/bin:/bin"},
    }))
    r = subprocess.run([sys.executable, "-m", "pasar.launch", str(tmp_path)],
                       capture_output=True, text=True, check=True)
    assert r.stdout.splitlines() == ["bar", str(work)]
