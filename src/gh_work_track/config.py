from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_HOME = Path.home() / ".local" / "share" / "gh-work-track"
DEFAULT_DB_DIR = "lance"

DEFAULT_REPO = "plaidev/karte-io-systems"
DEFAULT_MINE_REPOS = (
    "plaidev/karte-io-systems",
    "plaidev/karte-io-systems-ops",
    "otolab/my-logs",
)
DEFAULT_SYNC_BOOTSTRAP_DAYS = 7
DEFAULT_SYNC_OVERLAP_MINUTES = 5

CONFIG_ENV = "GH_WORK_TRACK_CONFIG"
XDG_CONFIG_ENV = "XDG_CONFIG_HOME"
CONFIG_DIR_NAME = "gh-work-track"
CONFIG_FILE_NAME = "config.yaml"

DEFAULT_REPO_ENV = "GH_WORK_TRACK_DEFAULT_REPO"
MINE_REPOS_ENV = "GH_WORK_TRACK_MINE_REPOS"
SYNC_BOOTSTRAP_DAYS_ENV = "GH_WORK_TRACK_SYNC_BOOTSTRAP_DAYS"
SYNC_OVERLAP_MINUTES_ENV = "GH_WORK_TRACK_SYNC_OVERLAP_MINUTES"


class ConfigError(ValueError):
    """A configuration file or environment override is invalid."""


@dataclass(frozen=True)
class SyncConfig:
    bootstrap_days: int = DEFAULT_SYNC_BOOTSTRAP_DAYS
    overlap_minutes: int = DEFAULT_SYNC_OVERLAP_MINUTES


@dataclass(frozen=True)
class Config:
    default_repo: str = DEFAULT_REPO
    mine_repos: tuple[str, ...] = DEFAULT_MINE_REPOS
    sync: SyncConfig = SyncConfig()


def config_path() -> Path:
    """Return the configured YAML path, honoring XDG and explicit override."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get(XDG_CONFIG_ENV)
    if config_home:
        return Path(config_home).expanduser() / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    return Path.home() / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _repo(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} は owner/repo 形式の文字列で指定してください")
    value = value.strip()
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
        raise ConfigError(f"{field_name} は owner/repo 形式で指定してください: {value!r}")
    return value


def _repo_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{field_name} は owner/repo の配列で指定してください")
    return tuple(_repo(item, f"{field_name}[{index}]") for index, item in enumerate(value))


def _positive_int(value: Any, field_name: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} は整数で指定してください")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} は整数で指定してください") from exc
    if isinstance(value, float) and value != parsed:
        raise ConfigError(f"{field_name} は整数で指定してください")
    if isinstance(value, str) and value.strip() != str(parsed):
        raise ConfigError(f"{field_name} は整数で指定してください")
    if parsed < minimum:
        raise ConfigError(f"{field_name} は {minimum} 以上で指定してください")
    return parsed


def _parse_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"設定ファイルを読み込めません: {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"設定ファイルの YAML が不正です: {path}: {exc}") from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigError(f"設定ファイルのトップレベルは mapping にしてください: {path}")
    return document


def load_config(path: Path | str | None = None) -> Config:
    """Load file settings merged onto application defaults.

    Environment overrides for individual values are applied by
    :func:`resolve_config`; this function represents the config-file layer.
    """
    config_file = Path(path).expanduser() if path is not None else config_path()
    document = _parse_document(config_file)

    default_repo = _repo(document.get("default_repo", DEFAULT_REPO), "default_repo")
    mine_repos = _repo_list(
        document.get("mine_repos", DEFAULT_MINE_REPOS),
        "mine_repos",
    )
    sync_document = document.get("sync", {})
    if sync_document is None:
        sync_document = {}
    if not isinstance(sync_document, dict):
        raise ConfigError("sync は mapping にしてください")
    sync = SyncConfig(
        bootstrap_days=_positive_int(
            sync_document.get("bootstrap_days", DEFAULT_SYNC_BOOTSTRAP_DAYS),
            "sync.bootstrap_days",
            minimum=1,
        ),
        overlap_minutes=_positive_int(
            sync_document.get("overlap_minutes", DEFAULT_SYNC_OVERLAP_MINUTES),
            "sync.overlap_minutes",
            minimum=0,
        ),
    )
    return Config(default_repo=default_repo, mine_repos=mine_repos, sync=sync)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _env_repos(value: str) -> tuple[str, ...]:
    repos = [part.strip() for part in re.split(r"[,\n]+", value) if part.strip()]
    return _repo_list(repos, MINE_REPOS_ENV)


def resolve_config(
    *,
    default_repo: str | None = None,
    mine_repos: Sequence[str] | None = None,
    bootstrap_days: int | None = None,
    overlap_minutes: int | None = None,
) -> Config:
    """Resolve CLI > environment > file > default configuration."""
    file_config = load_config()

    if default_repo is None:
        env_default_repo = _first_env(DEFAULT_REPO_ENV, "GH_WORK_TRACK_REPO")
        resolved_default_repo = (
            _repo(env_default_repo, DEFAULT_REPO_ENV)
            if env_default_repo is not None
            else file_config.default_repo
        )
    else:
        resolved_default_repo = _repo(default_repo, "--default-repo")

    if mine_repos is None:
        env_mine_repos = _first_env(MINE_REPOS_ENV, "GH_WORK_TRACK_MINE_REPO")
        resolved_mine_repos = (
            _env_repos(env_mine_repos)
            if env_mine_repos is not None
            else file_config.mine_repos
        )
    else:
        resolved_mine_repos = _repo_list(mine_repos, "--mine-repo")

    if bootstrap_days is None:
        env_bootstrap_days = _first_env(
            SYNC_BOOTSTRAP_DAYS_ENV,
            "GH_WORK_TRACK_BOOTSTRAP_DAYS",
        )
        resolved_bootstrap_days = (
            _positive_int(env_bootstrap_days, SYNC_BOOTSTRAP_DAYS_ENV, minimum=1)
            if env_bootstrap_days is not None
            else file_config.sync.bootstrap_days
        )
    else:
        resolved_bootstrap_days = _positive_int(
            bootstrap_days,
            "--bootstrap-days",
            minimum=1,
        )

    if overlap_minutes is None:
        env_overlap_minutes = _first_env(
            SYNC_OVERLAP_MINUTES_ENV,
            "GH_WORK_TRACK_OVERLAP_MINUTES",
        )
        resolved_overlap_minutes = (
            _positive_int(env_overlap_minutes, SYNC_OVERLAP_MINUTES_ENV, minimum=0)
            if env_overlap_minutes is not None
            else file_config.sync.overlap_minutes
        )
    else:
        resolved_overlap_minutes = _positive_int(
            overlap_minutes,
            "--overlap-minutes",
            minimum=0,
        )

    return Config(
        default_repo=resolved_default_repo,
        mine_repos=resolved_mine_repos,
        sync=SyncConfig(
            bootstrap_days=resolved_bootstrap_days,
            overlap_minutes=resolved_overlap_minutes,
        ),
    )


def data_home() -> Path:
    override = os.environ.get("GH_WORK_TRACK_HOME")
    if override:
        return Path(override).expanduser()
    return DEFAULT_HOME


def db_path(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    return data_home() / DEFAULT_DB_DIR
