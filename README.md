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

## Cloud jobs

Jobs can also run on rented GPUs instead of the local machine. It's off by default and costs
real money, so it's built to be hard to trigger by accident:

- A cloud target only exists if `config.toml` defines it with a `budget`, and needs its own
  spending limit set at the provider (its dashboard, not pasar) as the real backstop.
- Every cloud run needs a person's approval in the web UI (**Approve…** on its Cloud card), at
  its estimated cost, before it launches — there is no `pasar approve` command. A running job that
  needs longer gets **Give more time…** there too.
- One job may spend at most the target's `max_job_cost` (default $10) over its whole life, every
  attempt included; a submit estimated above it is refused. Raise it in `config.toml`.
- **Agents must only submit with `--on` when the user has explicitly asked for cloud compute for
  that work.**

To run jobs on [Modal](https://modal.com):

1. Install pasar with the Modal SDK: `uv tool install --force --reinstall '.[modal]'` from the
   clone (or `uv pip install 'pasar[modal]'` into the environment pasard runs from).
2. `modal token new`. pasar uses whatever credentials the Modal SDK finds. To keep several
   accounts apart, create a named profile instead (`modal token new --profile your-profile`) and
   name it on the target (`profile = "your-profile"`); a profile missing from `~/.modal.toml`
   leaves that target unusable rather than falling back to another account.
3. **Set a workspace spending limit in the Modal dashboard.** It is the one limit nothing on this
   machine can raise, and the only real boundary if something goes wrong. The SDK doesn't expose
   it, so pasar cannot check that you set one.
4. Add a target to `~/.config/pasar/config.toml`:

       [clouds.modal]
       provider = "modal"
       budget = { daily = 20.0 }   # USD; a target without budget.daily or budget.monthly
                                   # is a config error, and pasard won't start with it
       # profile = "your-profile"

   Running several Modal accounts from one pasard? Give each its own target with the same
   `group` (and a `profile` and `owner` each) instead, and submit with `--on <group>`; pasar
   picks the fullest account that still fits. See [docs/cloud.md](docs/cloud.md#running-from-several-accounts).
5. Restart pasard, then `pasar submit --on modal --gpu T4 --time 30m -- .venv/bin/python train.py`.
6. The job waits in `awaiting` until a person approves it with **Approve…** on the web UI's
   Cloud card. **Agents must never call the approve endpoint.**

When a cloud job finishes, whatever it wrote under `pasar_job.persist_dir()` is pulled to local
disk (`pull_dir`, by default `~/.local/share/pasar/pulls/<id>/`) and deleted at Modal. A pull
skipped for want of disk space (`pull_min_free`) leaves it at Modal for
`pasar pull <id> --to <dir>`, but only until `cloud_retention_days` (default 3) after the job
finished: then whatever is still there is deleted, and for a job nobody pulled that is the only
copy. **Storage is billed even when no compute is running**, and pasar's budgets
count compute only.

See [docs/cloud.md](docs/cloud.md) for the design and every setting, and
[`src/pasar/agents-cloud.md`](src/pasar/agents-cloud.md) (`pasar guide cloud`) for the rules
agents follow, including when a cloud GPU is worth asking for.

## For AI agents and API users

Start with the agent guide, [`src/pasar/agents.md`](src/pasar/agents.md). It is a self-contained
guide to connecting, submitting well-formed jobs, checkpointing, monitoring, the HTTP API
(endpoints and request bodies), and etiquette on a shared GPU. The copy that matches the installed
version is always one command or request away:

    pasar guide                                  # works even when pasard is down
    curl http://127.0.0.1:8750/llms.txt          # same text, served by pasard
    pasar guide cloud                            # the cloud GPU guide (/llms-cloud.txt)

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
