"""Configuration and paths (XDG)."""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("ELISA_TV_CONFIG_DIR")
                  or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "elisa-tv")
DATA_DIR = Path(os.environ.get("ELISA_TV_DATA_DIR")
                or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "elisa-tv")
CONFIG_PATH = CONFIG_DIR / "config.json"
AUTH_PATH = DATA_DIR / "auth.json"     # Elisa tokens (secret)
DB_PATH = DATA_DIR / "guide.db"
LOCK_PATH = DATA_DIR / "sync.lock"

DEFAULTS: dict = {
    "tv": {"host": None, "port": 5555, "name": None},
    "adb": None,
    "http": {"host": "0.0.0.0", "port": 8765, "api_key": None},
    # source: "account" (entitled channels of the logged-in Elisa account),
    #         "package" (bundled list, see packages.json), "custom" (channel_numbers), "all"
    "subscription": {"source": "all", "package": None, "channel_numbers": []},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load() -> dict:
    if CONFIG_PATH.exists():
        return _merge(DEFAULTS, json.loads(CONFIG_PATH.read_text()))
    return copy.deepcopy(DEFAULTS)


def write_secret(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def save(cfg: dict) -> None:
    write_secret(CONFIG_PATH, cfg)  # contains the API key


def adb_path(cfg: dict | None = None) -> str | None:
    cfg = cfg or load()
    for cand in (cfg.get("adb"), shutil.which("adb"), str(DATA_DIR / "platform-tools" / "adb")):
        if cand and Path(cand).exists():
            return cand
    return None


def packages() -> dict:
    return json.loads((Path(__file__).parent / "packages.json").read_text())
