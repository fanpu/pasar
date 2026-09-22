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

A web UI shows what's running, what's waiting, and why, alongside the command-line client — see
[Web UI](#web-ui) below.

## Install

Requires Linux with systemd, Python 3.11+, [uv](https://docs.astral.sh/uv/), and Node 20+ (only
needed once, to build the web UI).

    git clone https://github.com/fanpu/pasar
    cd pasar/web && npm ci && npm run build && cd ..
    uv tool install --force --reinstall .
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

## For AI agents

Tell your agent to run `pasar guide`, or fetch `http://127.0.0.1:8750/llms.txt` — it's a
self-contained guide to connecting, submitting well-formed jobs, checkpointing, monitoring, and
etiquette on a shared GPU, matching the version installed on this machine.

## Configure

`~/.config/pasar/config.toml` (all optional):

    bind = ["127.0.0.1:8750"]   # extra addresses to bind; 127.0.0.1:8750 is always included
    system_reserve = "16G"
    prometheus_url = "http://127.0.0.1:9090"
    allowed_hosts = ["mybox.example.ts.net"]   # extra Host-header names to accept

## Web UI

The web UI is built from a clone as part of [Install](#install) above (`cd web && npm ci && npm
run build`, then `uv tool install --force --reinstall .` — hatch bundles the built assets into the installed
tool). If you skipped it, or change the frontend later, rebuild and reinstall the same way; a
plain `uv run pasard` from the clone always serves whatever is currently built in `web/`, no
reinstall needed.

Once built, open `http://127.0.0.1:8750/` (or one of the extra `bind` addresses from your config;
add its DNS name to `allowed_hosts` if it isn't localhost).

For frontend development, run a Vite dev server instead — it hot-reloads and proxies `/api` and
`/mascot` to a running `pasard`:

    cd web && npm run dev

The dev server proxies to `PASAR_URL` (default `http://127.0.0.1:8750`). Pass `-- --host <addr>`
to reach it over a private network, and set `PASAR_DEV_HOSTS=name1,name2` for extra Host names it
should accept.

Custom mascot images go in `~/.config/pasar/mascot/`, named after the states listed in
[docs/design.md](docs/design.md).

### Browser smoke test

    cd web && npm run e2e

Runs a throwaway `pasard` (own port, own XDG dirs — never touches a real install) and drives it
with Playwright in real Chrome, checked at `/usr/bin/google-chrome`; set `PASAR_CHROME` to use a
different binary. It doesn't install browsers itself and needs a systemd user session, so it's
run by hand rather than in CI.

## Licence

MIT, see [LICENSE](LICENSE).
