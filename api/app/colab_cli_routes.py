"""Authenticated Colab CLI slice. Writes stay behind the existing CSRF middleware."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .colab_cli_adapter import CliDisabled
from .colab_cli_tasks import get_task, list_tasks, public_status, request_release, submit, usage_snapshot
from .common import APIError, current_user, ok

router = APIRouter(prefix="/api/gpu/colab-cli")


def _disabled(exc: CliDisabled) -> APIError:
    status = 409 if exc.code in {"CLI_DISABLED", "CLI_REAL_DISABLED", "CLI_GPU_DISABLED"} else 422
    return APIError(exc.code, exc.message, status)


@router.get("/status")
def status(_user=Depends(current_user)):
    return ok(public_status())


@router.get("/usage")
def usage(_user=Depends(current_user)):
    return ok(usage_snapshot())


@router.get("/tasks")
def tasks(_user=Depends(current_user)):
    return ok(list_tasks())


@router.get("/tasks/{task_id}")
def task(task_id: str, _user=Depends(current_user)):
    row = get_task(task_id)
    if not row:
        raise APIError("NOT_FOUND", "任务不存在", 404)
    return ok(row)


@router.post("/tasks")
def create_task(body: dict, _user=Depends(current_user)):
    if not isinstance(body, dict):
        raise APIError("VALIDATION_ERROR", "请求必须是 JSON 对象")
    script = body.get("script")
    accelerator = body.get("accelerator", "cpu")
    scenario = body.get("scenario", "") or ""
    if not isinstance(script, str) or not isinstance(accelerator, str) or not isinstance(scenario, str):
        raise APIError("VALIDATION_ERROR", "script、accelerator 和 scenario 必须是字符串")
    try:
        row = submit(script, accelerator, body.get("idle_seconds", 30), scenario)
    except CliDisabled as exc:
        raise _disabled(exc) from exc
    return ok(row, None, 202)


@router.post("/tasks/{task_id}/release")
def release_task(task_id: str, _user=Depends(current_user)):
    try:
        row = request_release(task_id)
    except KeyError as exc:
        raise APIError("NOT_FOUND", "任务不存在", 404) from exc
    except CliDisabled as exc:
        raise _disabled(exc) from exc
    return ok(row)
