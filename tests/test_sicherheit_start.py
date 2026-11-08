"""SEC-09/spec-05/ops-10 (access log without query strings, one log format), ops-03 (clear error
for a non-writable DATA_DIR), SEC-07 (refused start) – also end-to-end with ``python -m projektakte``."""
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from projektakte.config import load_settings
from projektakte.logsetup import LOG_FORMAT, DropHealthProbes, StripQueryString, logging_config
from projektakte.main import create_app
from projektakte.startup import ConfigError, check_data_dir, public_url, trusted_proxies

ROOT = Path(__file__).resolve().parent.parent


def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, "h11_impl.py", 1, '%s - "%s %s HTTP/%s" %d',
                             ("10.0.0.5:51234", "GET", path, "1.1", 200), None)


@pytest.mark.parametrize("path,expected", [
    ("/api/search?q=Was+ist+der+Stand%3F&project=BS-2023-0412&k=5", "/api/search"),
    ("/suche?q=Geheimprojekt+Mueller", "/suche"),
    ("/login?next=%2Fsuche%3Fq%3DGehalt", "/login"),
    ("/projekte/BS-2024-0137", "/projekte/BS-2024-0137"),
])
def test_access_log_without_query_string(path, expected):
    record = access_record(path)
    assert StripQueryString().filter(record)
    assert record.getMessage() == f'10.0.0.5:51234 - "GET {expected} HTTP/1.1" 200'


def test_successful_health_probes_not_logged():
    drop = DropHealthProbes()
    assert not drop.filter(access_record("/health/ready"))
    assert not drop.filter(access_record("/health"))
    failed = logging.LogRecord("uvicorn.access", logging.INFO, "h11_impl.py", 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:1", "GET", "/health/ready", "1.1", 503), None)
    assert drop.filter(failed) and drop.filter(access_record("/health/deps"))
    assert drop.filter(access_record("/suche?q=x"))


def test_logging_config_uses_one_format():
    cfg = logging_config("info")
    assert cfg["formatters"]["default"]["format"] == LOG_FORMAT and "%(asctime)s" in LOG_FORMAT
    assert cfg["loggers"]["uvicorn.access"]["filters"] == ["drop_probes", "strip_query"]
    assert all(lg["propagate"] for lg in cfg["loggers"].values())


def test_data_dir_not_writable(tmp_path):
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    with pytest.raises(ConfigError, match="DATA_DIR .* ist nicht beschreibbar"):
        check_data_dir(blocker / "data")
    with pytest.raises(ConfigError, match="chown"):
        create_app(load_settings({"DATA_DIR": str(blocker / "data"), "AUTH_DISABLED": "1"}), background=False)


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0, reason="root darf überall schreiben")
def test_data_dir_without_permission(tmp_path):
    locked = tmp_path / "gesperrt"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        with pytest.raises(ConfigError, match="nicht beschreibbar"):
            check_data_dir(locked)
    finally:
        locked.chmod(0o755)


def test_public_url_strips_credentials():
    assert public_url("http://nutzer:pw@llm.lan:8000/v1?key=abc") == "http://llm.lan:8000/v1"
    assert public_url("http://[fd00::1]:8001") == "http://[fd00::1]:8001"
    assert public_url("") == ""


def test_trusted_proxies(caplog):
    caplog.set_level(logging.WARNING)
    assert trusted_proxies(load_settings({})) == "127.0.0.1"
    s = load_settings({"FORWARDED_ALLOW_IPS": "172.31.250.10, 10.0.0.0/8, proxy.lan"})
    assert trusted_proxies(s, warn=True) == "172.31.250.10, 10.0.0.0/8, proxy.lan"
    assert "proxy.lan" in caplog.text and "172.31.250.10" not in caplog.text


# -- end to end: the real server process ---------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_module(env_extra: dict, tmp_path: Path, timeout: float = 30) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "DATA_DIR": str(tmp_path / "data"), **env_extra}
    return subprocess.run([sys.executable, "-m", "projektakte"], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=timeout)


def test_refused_start_is_a_clear_message(tmp_path):
    r = run_module({"APP_ENV": "production", "AUTH_DISABLED": "1"}, tmp_path)
    assert r.returncode == 2
    assert "Start abgebrochen: AUTH_DISABLED=1 ist nur mit APP_ENV=dev erlaubt" in r.stderr
    assert "Traceback" not in r.stderr
    r = run_module({"APP_ENV": "production", "AUTH_USERS": "", "SECRET_KEY": "kurz"}, tmp_path)
    assert r.returncode == 2 and "SECRET_KEY ist kürzer als 32 Zeichen" in r.stderr and "kurz\n" not in r.stderr
    blocker = tmp_path / "datei"
    blocker.write_text("x")
    r = run_module({"DATA_DIR": str(blocker / "data"), "AUTH_DISABLED": "1"}, tmp_path)
    assert r.returncode == 2 and "ist nicht beschreibbar" in r.stderr and "Traceback" not in r.stderr


def test_server_log_has_timestamps_and_no_search_terms(tmp_path):
    port = free_port()
    env = {"PATH": os.environ.get("PATH", ""), "DATA_DIR": str(tmp_path / "data"), "PORT": str(port),
           "APP_ENV": "dev", "AUTH_DISABLED": "1", "AI_MODE": "fake", "INDEX_INTERVAL": "0",
           "PROJECTS_DIR": str(tmp_path / "leer"), "MAIL_DIR": str(tmp_path / "leer"),
           "FORWARDED_ALLOW_IPS": "127.0.0.1"}
    proc = subprocess.Popen([sys.executable, "-m", "projektakte"], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(150):
            try:
                opener.open(base + "/health", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        opener.open(base + "/health?q=Geheimprojekt+Mueller", timeout=5).read()
        opener.open(base + "/login?next=%2Fsuche%3Fq%3DGeheimprojekt", timeout=5).read()
        opener.open(base + "/suche?q=Gehaltsliste+Brandschutz", timeout=30).read()
        req = urllib.request.Request(base + "/api/version", headers={"X-Forwarded-For": "10.20.30.40"})
        resp = opener.open(req, timeout=5)
        assert resp.headers["X-Frame-Options"] == "DENY" and "server" not in resp.headers
        time.sleep(0.3)
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=20)
    assert '"GET /suche HTTP/1.1" 200' in out and '"GET /login HTTP/1.1"' in out
    assert '"GET /health HTTP/1.1" 200' not in out  # successful probes are not logged
    assert "Geheimprojekt" not in out and "Gehaltsliste" not in out
    assert "10.20.30.40" in out  # client address from the trusted proxy header
    assert "Konfiguration: APP_ENV=dev, AI_MODE=fake" in out
    lines = [line for line in out.splitlines() if line.strip()]
    stamped = [line for line in lines if line[:4].isdigit() and line[4] == "-"]
    assert len(stamped) == len(lines), [line for line in lines if line not in stamped]
