"""config.py: the DB path lookup and the DATABASE_URL switch."""

from __future__ import annotations

import json

import pytest

import config


def test_unset_means_local_default(monkeypatch):
    assert config.db_target() == str(config.DEFAULT_DB_PATH)
    monkeypatch.setenv("DATABASE_URL", "   ")  # blank counts as unset
    assert config.db_target() == str(config.DEFAULT_DB_PATH)


def test_config_json_path_is_used(tmp_path):
    config.CONFIG_PATH.write_text(json.dumps({"db_path": str(tmp_path / "x.db")}), encoding="utf-8")
    assert config.db_target() == str(tmp_path / "x.db")
    config.CONFIG_PATH.write_text("{}", encoding="utf-8")  # no db_path key -> default
    assert config.db_target() == str(config.DEFAULT_DB_PATH)


def test_ensure_config_file_does_not_overwrite(tmp_path):
    config.ensure_config_file()
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8")) == {"db_path": str(config.DEFAULT_DB_PATH)}
    config.CONFIG_PATH.write_text(json.dumps({"db_path": "mine.db"}), encoding="utf-8")
    config.ensure_config_file()
    assert config.db_path() == "mine.db"


@pytest.mark.parametrize("url,expected", [
    ("sqlite:///relative.db", "relative.db"),
    ("sqlite:////abs/path/reviews.db", "/abs/path/reviews.db"),
])
def test_sqlite_urls(monkeypatch, url, expected):
    monkeypatch.setenv("DATABASE_URL", url)
    assert config.db_target() == expected
    assert not config.is_remote(config.db_target())


@pytest.mark.parametrize("url,expected", [
    ("libsql://steam-org.turso.io", "https://steam-org.turso.io"),
    ("LIBSQL://steam-org.turso.io/", "https://steam-org.turso.io"),
    ("https://steam-org.aws-us-east-1.turso.io", "https://steam-org.aws-us-east-1.turso.io"),
])
def test_turso_urls_need_token(monkeypatch, url, expected):
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ValueError, match="TURSO_AUTH_TOKEN"):
        config.db_target()
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    assert config.db_target() == expected
    assert config.is_remote(config.db_target())
    assert config.turso_auth_token() == "tok"


def test_local_server_needs_no_token(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "http://127.0.0.1:8080")
    assert config.db_target() == "http://127.0.0.1:8080"


@pytest.mark.parametrize("url", ["postgres://x/y", "reviews.db", "libsql://steam.turso.io?authToken=abc"])
def test_rejected_urls(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    with pytest.raises(ValueError):
        config.db_target()
