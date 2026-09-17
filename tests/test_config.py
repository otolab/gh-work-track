from __future__ import annotations

from gh_work_track.config import (
    CONFIG_ENV,
    DEFAULT_MINE_REPOS,
    DEFAULT_REPO,
    DEFAULT_SYNC_BOOTSTRAP_DAYS,
    DEFAULT_SYNC_OVERLAP_MINUTES,
    XDG_CONFIG_ENV,
    config_path,
    load_config,
    resolve_config,
)


CONFIG_VALUE_ENVIRONMENTS = (
    "GH_WORK_TRACK_DEFAULT_REPO",
    "GH_WORK_TRACK_REPO",
    "GH_WORK_TRACK_MINE_REPOS",
    "GH_WORK_TRACK_MINE_REPO",
    "GH_WORK_TRACK_SYNC_BOOTSTRAP_DAYS",
    "GH_WORK_TRACK_BOOTSTRAP_DAYS",
    "GH_WORK_TRACK_SYNC_OVERLAP_MINUTES",
    "GH_WORK_TRACK_OVERLAP_MINUTES",
)


def clear_config_environment(monkeypatch) -> None:
    for name in CONFIG_VALUE_ENVIRONMENTS:
        monkeypatch.delenv(name, raising=False)


def test_missing_config_uses_existing_defaults(monkeypatch, tmp_path):
    clear_config_environment(monkeypatch)
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "missing.yaml"))

    settings = resolve_config()

    assert settings.default_repo == DEFAULT_REPO
    assert settings.mine_repos == DEFAULT_MINE_REPOS
    assert settings.sync.bootstrap_days == DEFAULT_SYNC_BOOTSTRAP_DAYS
    assert settings.sync.overlap_minutes == DEFAULT_SYNC_OVERLAP_MINUTES


def test_config_path_honors_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.setenv(XDG_CONFIG_ENV, str(tmp_path / "xdg"))

    assert config_path() == tmp_path / "xdg" / "gh-work-track" / "config.yaml"


def test_load_config_reads_yaml_schema(monkeypatch, tmp_path):
    clear_config_environment(monkeypatch)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
default_repo: example/main
mine_repos:
  - example/one
  - example/two
sync:
  bootstrap_days: 14
  overlap_minutes: 2
""",
        encoding="utf-8",
    )

    settings = load_config(config_file)

    assert settings.default_repo == "example/main"
    assert settings.mine_repos == ("example/one", "example/two")
    assert settings.sync.bootstrap_days == 14
    assert settings.sync.overlap_minutes == 2


def test_cli_overrides_environment_and_config(monkeypatch, tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
default_repo: config/main
mine_repos: [config/repo]
sync:
  bootstrap_days: 14
  overlap_minutes: 2
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(CONFIG_ENV, str(config_file))
    monkeypatch.setenv("GH_WORK_TRACK_DEFAULT_REPO", "env/main")
    monkeypatch.setenv("GH_WORK_TRACK_MINE_REPOS", "env/one,env/two")
    monkeypatch.setenv("GH_WORK_TRACK_SYNC_BOOTSTRAP_DAYS", "21")
    monkeypatch.setenv("GH_WORK_TRACK_SYNC_OVERLAP_MINUTES", "4")

    from_environment = resolve_config()
    from_cli = resolve_config(
        default_repo="cli/main",
        mine_repos=["cli/repo"],
        bootstrap_days=28,
        overlap_minutes=6,
    )

    assert from_environment.default_repo == "env/main"
    assert from_environment.mine_repos == ("env/one", "env/two")
    assert from_environment.sync.bootstrap_days == 21
    assert from_environment.sync.overlap_minutes == 4
    assert from_cli.default_repo == "cli/main"
    assert from_cli.mine_repos == ("cli/repo",)
    assert from_cli.sync.bootstrap_days == 28
    assert from_cli.sync.overlap_minutes == 6
