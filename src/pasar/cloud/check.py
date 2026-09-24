"""Verify every configured cloud target still works: `pasar cloud check`.

Task 0's survey did this once, by hand, before the accounts plan was built at all: does each
profile still exist in `~/.modal.toml` and still authenticate, which workspace does it reach (a
loud warning if two targets reach the *same* one -- `headroom()` would then count one allowance
twice and the group would happily overspend), and how much of its credit is used. A token gets
revoked, a person leaves, an account's allowance changes -- and the failure mode without a
repeatable version of that survey is a job that launches and dies. This is that repeatable
version: read-only, spends nothing, never prints a token, and keeps working -- reporting rather
than raising -- when `modal` is not installed.

Independent of a running pasard on purpose: it reads `~/.config/pasar/config.toml` itself (see
`pasar.cli`), so it still works as a health check when pasard is down, which is exactly when a
person most wants to know whether a target is about to refuse every job.
"""

import importlib
import logging
from dataclasses import dataclass, field

from pasar.cloud.modal_profile import credentials
from pasar.cloud.modal_provider import credit_of
from pasar.config import CloudTarget

log = logging.getLogger(__name__)


@dataclass
class TargetCheck:
    """One target's own health. `ok` is False for anything that would make a job on it die: no
    profile configured, an unreadable `~/.modal.toml`, a token that no longer authenticates, or a
    workspace that could not be reached -- never for a billing summary that failed to read, since
    the ledger's own figures still gate every launch and a billing outage is not a reason to call
    the target unusable.

    `workspace` is set whenever the token authenticated, even if the billing summary read after
    it then failed, since authenticating is already enough to catch two targets sharing one
    workspace. `credit_used`/`budget_left`/`exhausted`/`cycle_start`/`cycle_end` are `None` (and
    `exhausted` False) when nothing could be read.

    `credit_used` and `exhausted` are Modal's own books, read exactly as the daemon reads them
    (`pasar.cloud.modal_provider.credit_of`). `budget_left` is not Modal's figure: it is the
    target's `monthly_budget` in pasar's config less `credit_used`, the same headroom the group
    choice works from, so an account whose real allowance differs from that budget is not
    described by it.

    `error` is never token text: `pasar.cloud.modal_profile.credentials` only ever names profiles
    and paths in what it raises, and every other failure here is reported by its exception type
    alone, never its message, since a provider exception's own text is not this module's to
    trust."""
    name: str
    provider: str
    owner: str
    group: str
    ok: bool
    error: str | None = None
    workspace: str | None = None
    credit_used: float | None = None
    budget_left: float | None = None
    exhausted: bool = False
    cycle_start: float | None = None
    cycle_end: float | None = None


@dataclass
class CloudCheckResult:
    targets: list[TargetCheck] = field(default_factory=list)
    # Workspace name -> every target name that reached it, one entry per workspace two or more
    # targets both reached.
    collisions: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """False if any target is unusable or any two share a workspace -- what `pasar cloud
        check`'s exit code reports, so it can be run from a health check."""
        return not self.collisions and all(t.ok for t in self.targets)


def _check_one(target: CloudTarget, sdk) -> TargetCheck:
    check = TargetCheck(name=target.name, provider=target.provider, owner=target.owner,
                        group=target.group, ok=False)
    try:
        token_id, token_secret = credentials(target.profile)
    except KeyError as e:
        check.error = str(e)
        return check
    try:
        # The cheap way to authenticate: it spends nothing, unlike a call that touches a
        # workspace's own resources. Broad on purpose -- one bad token must not stop every other
        # target in the same run being checked, and nothing here may raise past this row.
        server_url = sdk.config.config["server_url"]
        sdk.Client.verify(server_url, (token_id, token_secret))
        client = sdk.Client.from_credentials(token_id, token_secret)
    except Exception as e:  # noqa: BLE001 - reported per-target below, not this run's problem
        check.error = f"{type(e).__name__}: token does not authenticate"
        return check
    try:
        ws = sdk.Workspace.from_context(client=client)
        check.workspace = ws.name
    except Exception as e:  # noqa: BLE001 - reported per-target below, not this run's problem
        check.error = f"{type(e).__name__}: could not reach the workspace"
        return check
    check.ok = True
    try:
        credit = credit_of(ws.billing.summary())
    except Exception as e:  # noqa: BLE001 - a billing outage leaves the target ok, see above
        # The type alone, like every other failure here: a provider exception's own text is not
        # this module's to trust.
        log.warning("could not read %s's billing summary: %s", target.name, type(e).__name__)
        return check
    check.credit_used = credit.used
    check.exhausted = credit.exhausted
    check.cycle_start = credit.cycle_start
    check.cycle_end = credit.cycle_end
    check.budget_left = max(0.0, target.monthly_budget - credit.used)
    return check


def check_targets(targets: list[CloudTarget], sdk=None) -> CloudCheckResult:
    """One `TargetCheck` per target that names a `profile` -- a target with none has nothing here
    to verify, so it is left out entirely rather than reported as a failure. `sdk` stands in for
    the `modal` module in tests, the way `ModalProvider` takes one; production code leaves it
    `None` and gets a lazy import, so a pasar with no cloud target configured never needs `modal`
    installed at all."""
    with_profile = [t for t in targets if t.profile]
    if not with_profile:
        return CloudCheckResult()
    if sdk is None:
        try:
            sdk = importlib.import_module("modal")
        except ImportError:
            return CloudCheckResult(targets=[
                TargetCheck(name=t.name, provider=t.provider, owner=t.owner, group=t.group,
                           ok=False, error="modal is not installed")
                for t in with_profile])
    checks = [_check_one(t, sdk) for t in with_profile]
    by_workspace: dict[str, list[str]] = {}
    for c in checks:
        if c.workspace:
            by_workspace.setdefault(c.workspace, []).append(c.name)
    collisions = {ws: names for ws, names in by_workspace.items() if len(names) > 1}
    return CloudCheckResult(targets=checks, collisions=collisions)
