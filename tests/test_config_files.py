"""`<NAME>_FILE` secret support in solar.config."""
import pytest

from solar.config import Config, env


def test_default_and_plain_env(monkeypatch):
    monkeypatch.delenv("S2F_X", raising=False)
    monkeypatch.delenv("S2F_X_FILE", raising=False)
    assert env("S2F_X") is None
    assert env("S2F_X", "d") == "d"
    monkeypatch.setenv("S2F_X", "plain")
    assert env("S2F_X", "d") == "plain"


def test_file_wins_and_is_stripped(monkeypatch, tmp_path):
    f = tmp_path / "s"
    f.write_text("  from-file\n")
    monkeypatch.setenv("S2F_X", "plain")
    monkeypatch.setenv("S2F_X_FILE", str(f))
    assert env("S2F_X") == "from-file"


def test_empty_file_is_empty_not_default(monkeypatch, tmp_path):
    f = tmp_path / "s"
    f.write_text("")
    monkeypatch.setenv("S2F_X_FILE", str(f))
    assert env("S2F_X", "d") == ""


def test_missing_file_is_hard_error(monkeypatch, tmp_path):
    monkeypatch.setenv("S2F_X_FILE", str(tmp_path / "nope"))
    with pytest.raises(SystemExit, match="S2F_X_FILE"):
        env("S2F_X")


def test_config_reads_files(monkeypatch, tmp_path):
    host = tmp_path / "host"
    host.write_text("192.0.2.7\n")
    monkeypatch.delenv("SOLAR_INVERTER_HOST", raising=False)
    monkeypatch.setenv("SOLAR_INVERTER_HOST_FILE", str(host))
    assert Config.from_env().inverter_host == "192.0.2.7"
