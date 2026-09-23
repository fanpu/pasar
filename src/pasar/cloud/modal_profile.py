"""Read one Modal profile's credentials out of ~/.modal.toml.

pasar runs several accounts from one process, so it cannot use the SDK's notion of an *active*
profile: every call has to carry the client for the account whose credit is paying. The tokens
stay where `modal token new` put them — pasar's own config names a profile and nothing more.
"""

import os
import tomllib
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("MODAL_CONFIG_PATH") or Path.home() / ".modal.toml")


def credentials(profile: str) -> tuple[str, str]:
    """`(token_id, token_secret)` for `profile`.

    Never falls back to the active profile: a target that names an account it cannot find must
    fail loudly, because the quiet version of that bug runs one person's job on another person's
    credit. Nothing here is ever put in an exception message but the profile names themselves.
    """
    path = config_path()
    try:
        config = tomllib.loads(path.read_text())
    except OSError as e:
        raise KeyError(f"no Modal profiles in {path}: {e.strerror}") from None
    except ValueError:
        raise KeyError(f"{path} is not readable as TOML") from None
    section = config.get(profile)
    if section is None:
        raise KeyError(f"no Modal profile {profile!r} in {path}; it has "
                       f"{', '.join(sorted(config)) or 'none'}")
    missing = [k for k in ("token_id", "token_secret") if not section.get(k)]
    if missing:
        raise KeyError(f"Modal profile {profile!r} in {path} has no {' or '.join(missing)}; "
                       "run `modal token new --profile " + profile + "`")
    return str(section["token_id"]), str(section["token_secret"])
