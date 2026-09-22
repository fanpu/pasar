# pasar

A friendly GPU job scheduler for a single machine that is designed for agents

*Pasar* is Malay for "market", a nod to *pasar malams*, "night markets" that
are popular in Singapore (and also Malaysia and Indonesia). Unlike a literal
pasar malams, here jobs bid for time on the GPU to determine what to run next.

![The pasar dashboard: memory pool and GPU tiles, the schedule of running and projected jobs, and the job table](docs/screenshot.webp)

- **Bids set the queue order.** Default 1000; bid higher to go sooner. A big job waiting for room
  holds a reservation, so smaller jobs fill the gaps without delaying it.
- **Preemption only when asked for, with checkpoints.** A job submitted with `--preempt` stops
  lower-bid jobs (SIGTERM, grace period, SIGKILL) and requeues them; they resume from their
  checkpoints.
- **Whole GPU by default, or a memory slice.** Jobs get the whole GPU unless they suit sharing
  (CPU-heavy steps, small batches, I/O waits); then `--mem 24G` lets them share, and pasar adds a
  safety margin and watches real usage (including unified-memory GPUs, via NVML).
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

    pasar submit --time 2h --tag lr-sweep --note "lr sweep point 3" -- .venv/bin/python train.py
    pasar ls
    pasar logs -f 42
    pasar bid 42 1500                # go sooner
    pasar bid 42 2000 --preempt      # and stop lower-bid jobs to start now
    pasar cancel 42
    pasar wait 42 && echo done

See [docs/jobs.md](docs/jobs.md) for writing jobs that checkpoint and resume, and
[docs/design.md](docs/design.md) for how scheduling works.

## For AI agents and API users

Start with the agent guide, [`src/pasar/agents.md`](src/pasar/agents.md). It is a self-contained
guide to connecting, submitting well-formed jobs, checkpointing, monitoring, the HTTP API
(endpoints and request bodies), and etiquette on a shared GPU. The copy that matches the installed
version is always one command or request away:

    pasar guide                                  # works even when pasard is down
    curl http://127.0.0.1:8750/llms.txt          # same text, served by pasard

Submit work as granular as possible: one job per run. A hyperparameter sweep should be many
jobs (one per configuration), not one command that loops over every point. Small jobs give the
scheduler room to pack, preempt and retry them individually, and their time and memory estimates
are more accurate.

pasard listens on `127.0.0.1:8750`. To reach it from another machine, add an address to `bind`
and its DNS name to `allowed_hosts` (see [Configure](#configure)), then point the CLI at it with
`PASAR_URL=http://<host>:8750` or call the HTTP API there directly.

## Configure

`~/.config/pasar/config.toml` (all optional):

    bind = ["100.64.0.1:8750"]   # extra addresses (e.g. a Tailscale IP); 127.0.0.1:8750 is always on
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

pasar ships its own mascot sprites. To use your own, put images in `~/.config/pasar/mascot/`,
named after the states listed in [docs/design.md](docs/design.md).

### Browser smoke test

    cd web && npm run e2e

Runs a throwaway `pasard` (own port, own XDG dirs — never touches a real install) and drives it
with Playwright in real Chrome, checked at `/usr/bin/google-chrome`; set `PASAR_CHROME` to use a
different binary. It doesn't install browsers itself and needs a systemd user session, so it's
run by hand rather than in CI.

## Licence

MIT, see [LICENSE](LICENSE).
