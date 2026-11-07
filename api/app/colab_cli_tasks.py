"""One owned Colab CLI session per script task. Release is proven by `sessions`."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .colab_cli_adapter import (
    CliDisabled,
    endpoint_for,
    exec_script,
    mode,
    new_session,
    release_proven,
    root_dir,
    usage_view,
    validate_accelerator,
    validate_session_name,
)

_LOCK = threading.RLock()
_TASKS: dict[str, dict] = {}
_LOADED = False
_BOUND = ""
_SCENARIOS = {"ok", "auth", "release-unproven", "usage-down", "exec-fail"}


def _path() -> Path:
    return root_dir() / "tasks.json"


def _load() -> None:
    global _LOADED, _BOUND
    current = str(root_dir())
    if _LOADED and _BOUND == current:
        return
    _TASKS.clear()
    _BOUND = current
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = []
    if isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                _TASKS[row["id"]] = row
    _LOADED = True
    for task in list(_TASKS.values()):
        if task.get("state") in {"provisioning", "running", "releasing"}:
            task["state"] = "residual"
            task["residual"] = True
            task["residual_reason"] = "进程重启时任务尚未结束。只会对本任务的 studio- 会话重试释放。"
            _try_release(task)
    _save()


def _save() -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(_TASKS.values()), ensure_ascii=False, indent=2), encoding="utf-8")


def _try_release(task: dict) -> None:
    name = task.get("session_name") or ""
    try:
        validate_session_name(name)
    except CliDisabled:
        return
    if mode() in {"off", "blocked"}:
        return
    proven, reason = release_proven(name, task.get("endpoint") or "")
    task["residual"] = not proven
    task["residual_reason"] = "" if proven else reason
    if proven and task.get("state") == "residual":
        task["state"] = "cancelled" if task.get("cancel") else "failed"


def public_status() -> dict:
    with _LOCK:
        _load()
    current = mode()
    notices = {
        "fake": "模拟模式：不会申请真实 Colab 运行时，也不会消耗 CU。",
        "off": "适配器关闭。不会调用 colab，也不会申请运行时。",
        "blocked": "真实模式被拒绝：需要 GPU_REAL_ENABLED=1 且 COLAB_CLI_ALLOW_REAL=1。",
        "real": "真实模式已放行。选择 GPU 会向 Colab 申请对应加速器。",
    }
    return {
        "mode": current,
        "fake": current == "fake",
        "label": {"fake": "模拟", "off": "关闭", "blocked": "未放行", "real": "真实"}.get(current, current),
        "real_allowed": current == "real",
        "gpu_real_enabled": os.getenv("GPU_REAL_ENABLED", "0").strip() == "1",
        "notice": notices.get(current, ""),
    }


def list_tasks() -> list[dict]:
    with _LOCK:
        _load()
        return [_public(row) for row in _TASKS.values()]


def get_task(task_id: str) -> dict | None:
    with _LOCK:
        _load()
        row = _TASKS.get(task_id)
        return _public(row) if row else None


def _public(row: dict) -> dict:
    keys = (
        "id",
        "state",
        "accelerator",
        "session_name",
        "endpoint",
        "exit_code",
        "log",
        "usage",
        "residual",
        "residual_reason",
        "auth_blocker",
        "fake",
    )
    return {key: row.get(key) for key in keys}


def submit(script: str, accelerator: str, idle_seconds: int, scenario: str) -> dict:
    current = mode()
    if current in {"off", "blocked"}:
        raise CliDisabled(
            "CLI_DISABLED" if current == "off" else "CLI_REAL_DISABLED",
            public_status()["notice"],
        )
    if not isinstance(script, str) or not script.strip() or "\x00" in script or len(script) > 65536:
        raise CliDisabled("CLI_SCRIPT", "脚本必须是不超过 64KB 的文本，且只会被送到 Colab，不会在本机执行。")
    chosen = validate_accelerator(accelerator, allow_gpu=current == "real")
    if scenario and (current != "fake" or scenario not in _SCENARIOS):
        raise CliDisabled("CLI_SCENARIO", "未知的模拟场景")
    try:
        idle = int(idle_seconds)
    except (TypeError, ValueError) as exc:
        raise CliDisabled("CLI_TIMEOUT", "空闲超时必须是整数秒") from exc
    if not 5 <= idle <= 3600:
        raise CliDisabled("CLI_TIMEOUT", "空闲超时必须在 5 到 3600 秒之间")
    task_id = uuid.uuid4().hex
    work = root_dir() / "work" / task_id
    work.mkdir(parents=True, exist_ok=True)
    script_path = work / "script.py"
    script_path.write_text(script, encoding="utf-8")
    row = {
        "id": task_id,
        "state": "queued",
        "accelerator": chosen,
        "session_name": f"studio-{task_id}",
        "endpoint": "",
        "exit_code": None,
        "log": "",
        "usage": {"available": False, "reason": "not-fetched"},
        "residual": False,
        "residual_reason": "",
        "auth_blocker": "",
        "fake": current == "fake",
        "cancel": False,
        "script_path": str(script_path),
        "idle_seconds": idle,
        "scenario": scenario or "",
    }
    with _LOCK:
        _load()
        _TASKS[task_id] = row
        _save()
    threading.Thread(target=_run, args=(task_id,), daemon=True).start()
    return _public(row)


def request_release(task_id: str) -> dict:
    from .colab_cli_adapter import kill_task

    with _LOCK:
        _load()
        task = _TASKS.get(task_id)
        if not task:
            raise KeyError(task_id)
        task["cancel"] = True
        _save()
    kill_task(task_id)
    with _LOCK:
        _try_release(task)
        if task.get("residual"):
            task["state"] = "residual"
        elif task.get("state") in {"queued", "provisioning", "running", "releasing"}:
            task["state"] = "cancelled"
        _save()
        return _public(task)


def _append(task: dict, text: str) -> None:
    task["log"] = ((task.get("log") or "") + text)[-20000:]


def _apply_scenario(task: dict) -> None:
    scenario = task.get("scenario") or ""
    if scenario and mode() == "fake":
        os.environ["COLAB_CLI_FAKE_SCENARIO"] = scenario
    elif "COLAB_CLI_FAKE_SCENARIO" in os.environ and mode() == "fake":
        os.environ.pop("COLAB_CLI_FAKE_SCENARIO", None)


def _run(task_id: str) -> None:
    with _LOCK:
        task = _TASKS[task_id]
    _apply_scenario(task)
    exit_code = 1
    cancelled = False
    try:
        usage = usage_view()
        with _LOCK:
            task["usage"] = {key: value for key, value in usage.items() if key != "detail"}
            if usage.get("auth_blocker"):
                task["auth_blocker"] = usage["auth_blocker"]
                task["state"] = "blocked"
                _save()
                return
            task["state"] = "provisioning"
            _save()
        created = new_session(task["session_name"], task["accelerator"], task_id)
        with _LOCK:
            _append(task, created.text + "\n")
            if created.auth_blocker:
                task["auth_blocker"] = created.auth_blocker
                task["state"] = "blocked"
                _save()
                return
            if created.code != 0:
                task["state"] = "failed"
                task["exit_code"] = created.code
                _save()
                return
            task["endpoint"] = endpoint_for(task["session_name"])
            if task.get("cancel"):
                _finish_release(task, exit_code=None, cancelled=True)
                return
            task["state"] = "running"
            _save()
        ran = exec_script(task["session_name"], Path(task["script_path"]), int(task["idle_seconds"]), task_id)
        exit_code = ran.code
        with _LOCK:
            _append(task, ran.text + "\n")
            task["exit_code"] = exit_code
            cancelled = bool(task.get("cancel"))
        _finish_release(task, exit_code=exit_code, cancelled=cancelled)
    except CliDisabled as exc:
        with _LOCK:
            task["state"] = "failed"
            _append(task, exc.message + "\n")
            _save()
    except Exception:
        with _LOCK:
            task["exit_code"] = 1
            _finish_release(task, exit_code=1, cancelled=False)


def _finish_release(task: dict, exit_code: int | None, cancelled: bool) -> None:
    task["state"] = "releasing"
    _save()
    proven, reason = release_proven(task["session_name"], task.get("endpoint") or "")
    task["residual"] = not proven
    task["residual_reason"] = "" if proven else reason
    if not proven:
        task["state"] = "residual"
    elif cancelled:
        task["state"] = "cancelled"
    elif exit_code == 0:
        task["state"] = "succeeded"
    else:
        task["state"] = "failed"
    _save()


def usage_snapshot() -> dict:
    current = mode()
    if current in {"off", "blocked"}:
        return {"available": False, "reason": current}
    try:
        view = usage_view()
    except CliDisabled as exc:
        return {"available": False, "reason": exc.code}
    view.pop("detail", None)
    return view
