# pasar

A friendly GPU job scheduler for a single machine shared by a small, cooperative group (and
the agents they steer).

- **Bids set priority.** Default 1000. Bid higher only when the work is worth preempting others.
- **Preemption with checkpoints.** Higher bids stop lower ones (SIGTERM, grace period, SIGKILL)
  and requeue them; jobs resume from their checkpoints.
- **Whole GPU or a memory slice.** `--mem 24G` lets jobs share the GPU; pasar adds a safety margin
  and watches real usage (including unified-memory GPUs, via NVML).
- **Knows why jobs died.** Out of memory, crashes, GPU errors, with the last log lines.
- **Counts lost time** from preemptions and failures.
- **Agent-friendly.** Every command has `--json`; `pasar wait` exit codes say what happened.

A web UI is planned but not built yet; today pasar is a daemon plus a command-line client.

## Install

Requires Linux with systemd, Python 3.11+, and [uv](https://docs.astral.sh/uv/).

    uv tool install git+https://github.com/fanpu/pasar
    mkdir -p ~/.config/systemd/user
    curl -o ~/.config/systemd/user/pasard.service \
        https://raw.githubusercontent.com/fanpu/pasar/main/contrib/pasard.service
    systemctl --user enable --now pasard
    loginctl enable-linger "$USER"     # keep pasard running when you log out

## Use

    pasar submit --time 2h --mem 24G --note "lr sweep point 3" -- .venv/bin/python train.py
    pasar ls
    pasar logs -f 42
    pasar bid 42 1500
    pasar cancel 42
    pasar wait 42 && echo done

See [docs/jobs.md](docs/jobs.md) for writing jobs that checkpoint and resume, and
[docs/design.md](docs/design.md) for how scheduling works.

## Configure

`~/.config/pasar/config.toml` (all optional):

    bind = ["127.0.0.1:8750"]   # extra addresses to bind; 127.0.0.1:8750 is always included
    system_reserve = "16G"
    prometheus_url = "http://127.0.0.1:9090"
    allowed_hosts = ["mybox.example.ts.net"]   # extra Host-header names to accept

## Web UI

Build the static assets that `pasard` serves:

    cd web && npm ci && npm run build

Then open `http://127.0.0.1:8750/` (or one of the extra `bind` addresses from your config; add
its DNS name to `allowed_hosts` if it isn't localhost).

For frontend development, run a Vite dev server instead — it hot-reloads and proxies `/api` and
`/mascot` to a running `pasard`:

    cd web && npm run dev

The dev server proxies to `PASAR_URL` (default `http://127.0.0.1:8750`). Pass `-- --host <addr>`
to reach it over a private network, and set `PASAR_DEV_HOSTS=name1,name2` for extra Host names it
should accept.

Custom mascot images go in `~/.config/pasar/mascot/`, named after the states listed in
[docs/design.md](docs/design.md).

## Licence

MIT, see [LICENSE](LICENSE).
