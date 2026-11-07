"""Colab CLI slice. The fake executable never contacts Google or runs the script."""

import json
import stat
import time
from pathlib import Path

from app.config import settings
from app.db import Store
from app.main import _login_limits, app
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"
SECRET = "test-session-private-signing-key-value"
FAKE = Path(__file__).resolve().parents[1] / "scripts" / "colab_cli_fake.py"
MARKER = Path("/tmp/studio-colab-cli-must-not-run")


def _login(client):
    response = client.post("/api/auth/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": "Bearer " + token}


def _wait(client, headers, task_id):
    last = {}
    for _ in range(80):
        response = client.get(f"/api/gpu/colab-cli/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        last = response.json()["data"]
        if last["state"] not in {"queued", "provisioning", "running", "releasing"}:
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def _trace(tmp_path):
    path = tmp_path / "colab-cli" / "argv-trace.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _commands(trace):
    commands = []
    for argv in trace:
        assert isinstance(argv, list)
        assert all(isinstance(item, str) for item in argv)
        joined = " ".join(argv)
        assert "bash" not in joined and "sh -c" not in joined
        if "usage" in argv:
            commands.append("usage")
        elif "new" in argv:
            commands.append("new")
        elif "exec" in argv:
            commands.append("exec")
        elif "stop" in argv:
            commands.append("stop")
        elif "sessions" in argv:
            commands.append("sessions")
    return commands


class TestColabCliSlice:
    def setup_method(self):
        MARKER.unlink(missing_ok=True)

    def _client(self, tmp_path, monkeypatch, *, mode="fake"):
        app.state.store = Store("sqlite:///" + str(tmp_path / "cli.db"))
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "studio_password", PASSWORD)
        monkeypatch.setattr(settings, "session_secret", SECRET)
        monkeypatch.setattr(settings, "site_url", "http://localhost:8788")
        monkeypatch.setattr(settings, "studio_api_token", "")
        monkeypatch.setenv("GPU_REAL_ENABLED", "0")
        monkeypatch.setenv("COLAB_CLI_ALLOW_REAL", "0")
        monkeypatch.setenv("COLAB_CLI_MODE", mode)
        monkeypatch.setenv("COLAB_CLI_BIN", str(FAKE))
        monkeypatch.delenv("COLAB_CLI_FAKE_SCENARIO", raising=False)
        mode_bits = FAKE.stat().st_mode
        FAKE.chmod(mode_bits | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _login_limits.clear()
        return TestClient(app)

    def test_adapter_is_off_by_default_and_real_mode_cannot_bypass_gpu_flag(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch, mode="off") as client:
            headers = _login(client)
            status = client.get("/api/gpu/colab-cli/status", headers=headers)
            assert status.status_code == 200
            assert status.json()["data"]["mode"] == "off"
            rejected = client.post("/api/gpu/colab-cli/tasks", headers=headers, json={"script": "print(1)\n"})
            assert rejected.status_code == 409
            assert rejected.json()["code"] == "CLI_DISABLED"
            assert _trace(tmp_path) == []
        monkeypatch.setenv("COLAB_CLI_MODE", "real")
        with self._client(tmp_path, monkeypatch, mode="real") as client:
            headers = _login(client)
            rejected = client.post(
                "/api/gpu/colab-cli/tasks",
                headers=headers,
                json={"script": "print(1)\n", "accelerator": "A100"},
            )
            assert rejected.status_code == 409
            assert rejected.json()["code"] == "CLI_REAL_DISABLED"
            assert _trace(tmp_path) == []
            assert not MARKER.exists()

    def test_fake_run_releases_only_its_session_and_does_not_execute_locally(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            headers = _login(client)
            sessions = tmp_path / "colab-cli" / "sessions.json"
            sessions.parent.mkdir(parents=True, exist_ok=True)
            sessions.write_text(
                json.dumps({"other-person": {"endpoint": "ep-other", "token": "ya29.OTHER", "accelerator": "NONE"}}),
                encoding="utf-8",
            )
            script = "import pathlib\npathlib.Path('/tmp/studio-colab-cli-must-not-run').write_text('pwned')\nprint('$(rm -rf /)')\n"
            created = client.post(
                "/api/gpu/colab-cli/tasks",
                headers=headers,
                json={"script": script, "accelerator": "cpu", "idle_seconds": 30},
            )
            assert created.status_code == 202, created.text
            task = _wait(client, headers, created.json()["data"]["id"])
            assert task["state"] == "succeeded"
            assert task["fake"] is True
            assert task["usage"]["available"] is True
            assert task["usage"]["balance"] == 12.5
            assert task["usage"]["rate_hourly"] == 1.25
            assert task["usage"]["source"] == "colab-cli"
            assert task["residual"] is False
            assert "ya29.FAKE-TOKEN-VALUE" not in json.dumps(task)
            assert "executed-remote" in task["log"]
            assert not MARKER.exists()
            commands = _commands(_trace(tmp_path))
            assert commands == ["usage", "new", "exec", "stop", "sessions"]
            stop = next(argv for argv in _trace(tmp_path) if "stop" in argv)
            assert "-s" in stop
            assert stop[stop.index("-s") + 1] == task["session_name"]
            assert "other-person" not in stop
            stored = json.loads(sessions.read_text(encoding="utf-8"))
            assert "other-person" in stored
            assert task["session_name"] not in stored

    def test_unproven_release_is_visible_and_retry_is_limited_to_that_session(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            headers = _login(client)
            created = client.post(
                "/api/gpu/colab-cli/tasks",
                headers=headers,
                json={"script": "print(1)\n", "scenario": "release-unproven"},
            )
            task = _wait(client, headers, created.json()["data"]["id"])
            assert task["state"] == "residual"
            assert task["residual"] is True
            assert "不能当作释放成功" in task["residual_reason"]
            retried = client.post(f"/api/gpu/colab-cli/tasks/{task['id']}/release", headers=headers)
            assert retried.status_code == 200, retried.text
            body = retried.json()["data"]
            assert body["residual"] is False
            assert body["session_name"].startswith("studio-")

    def test_auth_blocker_is_not_reported_as_success(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            headers = _login(client)
            created = client.post(
                "/api/gpu/colab-cli/tasks",
                headers=headers,
                json={"script": "print(1)\n", "scenario": "auth"},
            )
            task = _wait(client, headers, created.json()["data"]["id"])
            assert task["state"] == "blocked"
            assert "gcloud auth application-default login" in task["auth_blocker"]
            assert task["state"] != "succeeded"
            assert "new" not in _commands(_trace(tmp_path))

    def test_missing_usage_is_not_invented(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            headers = _login(client)
            created = client.post(
                "/api/gpu/colab-cli/tasks",
                headers=headers,
                json={"script": "print(1)\n", "scenario": "usage-down"},
            )
            task = _wait(client, headers, created.json()["data"]["id"])
            assert task["usage"]["available"] is False
            assert "balance" not in task["usage"]
            assert "1000" not in json.dumps(task["usage"])

    def test_mutations_keep_login_csrf_and_cookie_write_rules(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            headers = _login(client)
            cookie_only = client.post("/api/gpu/colab-cli/tasks", json={"script": "print(1)\n"})
            assert cookie_only.status_code == 401
            evil = client.post(
                "/api/gpu/colab-cli/tasks",
                headers={**headers, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
                json={"script": "print(1)\n"},
            )
            assert evil.status_code == 403
            assert evil.json()["code"] == "CSRF_ORIGIN"
            assert _trace(tmp_path) == []

    def test_gpu_choice_is_rejected_while_real_allocation_is_disabled(self, tmp_path, monkeypatch):
        with self._client(tmp_path, monkeypatch) as client:
            headers = _login(client)
            rejected = client.post(
                "/api/gpu/colab-cli/tasks",
                headers=headers,
                json={"script": "print(1)\n", "accelerator": "H100"},
            )
            assert rejected.status_code == 409
            assert rejected.json()["code"] == "CLI_GPU_DISABLED"
            assert _trace(tmp_path) == []
