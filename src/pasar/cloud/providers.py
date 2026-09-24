"""Build the providers a config's cloud targets ask for, without needing any of their SDKs
installed unless a target actually uses one."""

import logging
from pathlib import Path

from pasar.config import CloudTarget, Config

log = logging.getLogger(__name__)

INSTALL_HINT = {"modal": "if modal is not installed, install it with `uv pip install "
                         "'pasar[modal]'`; if it is, check ~/.modal.toml has exactly one active "
                         "profile and the target's `profile`; then restart pasard"}


def _MODAL(target: CloudTarget, state_dir: Path):
    # Imported inside the factory, so a pasar with no Modal target never needs modal installed.
    from pasar.cloud.modal_provider import ModalProvider

    return ModalProvider(target, state_dir)


# Looked up through the module global rather than bound here, so a test can replace _MODAL.
_FACTORIES = {"modal": lambda target, state_dir: _MODAL(target, state_dir)}


def build_providers(cfg: Config, data_dir: Path) -> dict[str, object]:
    """One provider per configured target, skipping any that cannot be built.

    A target that is skipped is left out of the daemon's executors entirely, which is the case
    `Daemon._drop_unreachable` handles: a running job is failed, since nothing can follow it, and
    waiting ones are left where they are until the provider is back. That is much better than
    refusing to start pasard, which would take every local job down with it over a cloud target
    nobody is using today.
    """
    providers: dict[str, object] = {}
    for name, target in cfg.clouds.items():
        factory = _FACTORIES.get(target.provider)
        if factory is None:
            if target.provider_from_group:
                # Nobody set `provider =` on any member, so the group's own name stood in for
                # it — and that name is not a provider pasar has. Say exactly what to change,
                # since "provider %r" alone would point at the group name, not the fix.
                known = ", ".join(sorted(_FACTORIES)) or "a real provider"
                log.warning("cloud target %s inherited provider %r from group %r, which pasar "
                            "does not have; jobs cannot be submitted to it — set provider = on "
                            "every member of group %r (e.g. %s)", name, target.provider,
                            target.group, target.group, known)
            else:
                log.warning("cloud target %s asks for provider %r, which pasar does not have; "
                            "jobs cannot be submitted to it", name, target.provider)
            continue
        try:
            providers[name] = factory(target, Path(data_dir) / "cloud" / name)
        except Exception as e:  # noqa: BLE001 - a broken cloud target must never stop pasard
            hint = INSTALL_HINT.get(target.provider, "check its configuration")
            log.warning("cloud target %s could not be set up (%s); %s", name, e, hint)
    return providers
