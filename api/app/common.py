"""Shared authenticated API primitives; no import of the application composition root."""

import secrets
import uuid
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import settings
from .security import SESSION_COOKIE, read_session


class APIError(Exception):
    def __init__(self, code, message, status=422, retryable=False):
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


def envelope(data=None, error=None, request_id=None):
    return {"data": data, "error": error, "request_id": request_id or str(uuid.uuid4())}


def ok(data, request=None, status=200):
    return JSONResponse(
        envelope(data, request_id=getattr(getattr(request, "state", None), "request_id", None)),
        status_code=status,
    )


def get_store(request: Request):
    return request.app.state.store


def same_secret(supplied: str, expected: str) -> bool:
    if not supplied or not expected or len(supplied) != len(expected):
        return False
    return secrets.compare_digest(supplied, expected)


def _origin_allowed(request: Request, origin: str) -> bool:
    if not origin or origin == "null":
        return False
    if origin.rstrip("/") == settings.site_url.rstrip("/"):
        return True
    return urlsplit(origin).netloc == request.url.netloc


def cross_site_error(request: Request) -> str | None:
    """Reject a request the browser marks as cross-site.

    A missing Origin, Referer, or Sec-Fetch-Site header is not treated as proof of
    same-origin. Non-browser clients omit them. Possession of the cookie itself is
    still enough for those clients, because the cookie value is the bearer.
    """
    if request.headers.get("sec-fetch-site", "").strip().casefold() == "cross-site":
        return "跨站请求被拒绝"
    origin = request.headers.get("origin")
    if origin is not None and not _origin_allowed(request, origin.strip()):
        return "跨站请求被拒绝"
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc and not _origin_allowed(request, f"{parts.scheme}://{parts.netloc}"):
            return "跨站请求被拒绝"
    return None


def current_user(request: Request):
    authorization = request.headers.get("Authorization", "")
    raw = authorization[7:] if authorization.startswith("Bearer ") else ""
    if same_secret(raw, settings.studio_api_token):
        return {"id": "owner", "role": "admin", "api_client": True}
    if not raw and request.method in {"GET", "HEAD"}:
        raw = request.cookies.get(SESSION_COOKIE, "")
    payload = read_session(raw)
    if not payload or not payload.get("uid"):
        raise APIError("UNAUTHENTICATED", "请先登录独立 GPU 工作台", 401)
    return {"id": payload["uid"], "role": payload.get("role", "user")}


def own(st, kind, record_id, user):
    value = st.get(kind, record_id, user["id"])
    if not value:
        raise APIError("NOT_FOUND", "资源不存在或不属于当前用户", 404)
    return value


def admin(user):
    if user.get("role") != "admin":
        raise APIError("FORBIDDEN", "此操作需要管理员确认", 403)
