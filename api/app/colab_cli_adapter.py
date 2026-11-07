"""Official Colab CLI adapter. The binary is invoked with a fixed argv list.

Nothing in a user script is interpolated into a shell. Real allocation is
refused unless GPU_REAL_ENABLED=1 and COLAB_CLI_ALLOW_REAL=1.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import settings

OWNED_SESSION = re.compile(r"^studio-[0-9a-f]{32}$")
GPUS = ("T4", "L4", "G4", "H100", "A100")
_TOKEN_RE = re.compile(
    r"(ya29\.[0-9A-Za-z_\-]+|Bearer\s+\S+|\"token\"\s*:\s*\"[^\"]*\"|\"url\"\s*:\s*\"[^\"]*\")",
    re.IGNORECASE,
)
_AUTH_MARKERS = (
    "No valid default credentials found",
    "To authorize colab-cli, visit this URL",
    "Enter the authorization code",
    "gcloud auth application-default login",
)
_ENV_ALLOW = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TZ",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_CONFIG",
    "CLOUDSDK_PYTHON",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


class CliDisabled(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass
class CommandResult:
    argv: list[str]
    code: int
    text: str
    auth_blocker: str | None


def mode() -> str:
    """off, fake, real, or blocked (real requested without the two opt-in flags)."""
    requested = os.getenv("COLAB_CLI_MODE", "off").strip().lower()
    if requested in {"", "off", "0", "false"}:
        return "off"
    if requested == "fake":
        return "fake"
    if requested == "real":
        real_gpu = os.getenv("GPU_REAL_ENABLED", "0").strip() == "1"
        allow = os.getenv("COLAB_CLI_ALLOW_REAL", "0").strip() == "1"
        return "real" if real_gpu and allow else "blocked"
    return "off"


def redact(text: str) -> str:
    cleaned = _TOKEN_RE.sub("[REDACTED]", text or "")
    return cleaned[-20000:]


def auth_blocker(text: str) -> str | None:
    if any(marker in text for marker in _AUTH_MARKERS):
        return redact(text).strip()[:4000] or "Colab CLI requires authentication."
    return None


def validate_session_name(name: str) -> str:
    if not OWNED_SESSION.fullmatch(name or ""):
        raise CliDisabled("CLI_SESSION", "只能释放本工作台创建的 studio- 会话")
    return name


def validate_accelerator(value: str, *, allow_gpu: bool) -> str:
    name = (value or "cpu").strip()
    if name == "cpu":
        return "cpu"
    if name not in GPUS:
        raise CliDisabled("CLI_ACCELERATOR", "加速器必须是 cpu、T4、L4、G4、H100 或 A100")
    if not allow_gpu:
        raise CliDisabled("CLI_GPU_DISABLED", "真实 GPU 未开启。当前只会接受 cpu，避免申请计费运行时。")
    return name


def root_dir() -> Path:
    path = Path(settings.data_dir) / "colab-cli"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _executable() -> str:
    current = mode()
    if current == "off":
        raise CliDisabled("CLI_DISABLED", "Colab CLI 适配器默认关闭。设置 COLAB_CLI_MODE=fake 或按文档开启 real。")
    if current == "blocked":
        raise CliDisabled(
            "CLI_REAL_DISABLED",
            "真实 CLI 需要同时设置 GPU_REAL_ENABLED=1 和 COLAB_CLI_ALLOW_REAL=1。当前不会申请运行时。",
        )
    configured = os.getenv("COLAB_CLI_BIN", "").strip()
    if current == "fake" and not configured:
        raise CliDisabled("CLI_BIN", "模拟模式必须设置 COLAB_CLI_BIN，指向不联网的替身程序。")
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        import shutil

        found = shutil.which("colab")
        if not found:
            raise CliDisabled("CLI_BIN", "PATH 中没有 colab。Linux/macOS 可安装 google-colab-cli。")
        path = Path(found).resolve()
    if not path.is_file():
        raise CliDisabled("CLI_BIN", "COLAB_CLI_BIN 不是可执行文件")
    return str(path)


def _config_path() -> Path:
    return root_dir() / "sessions.json"


def _trace(argv: list[str]) -> None:
    line = json.dumps(argv, ensure_ascii=False)
    with (root_dir() / "argv-trace.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _env() -> dict[str, str]:
    if mode() == "fake":
        home = root_dir() / "fake-home"
        home.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(home), "LANG": "C.UTF-8"}
        scenario = os.getenv("COLAB_CLI_FAKE_SCENARIO", "").strip()
        if scenario:
            env["COLAB_CLI_FAKE_SCENARIO"] = scenario
        return env
    return {key: os.environ[key] for key in _ENV_ALLOW if os.environ.get(key)}


def _prefix() -> list[str]:
    argv = [_executable(), "--auth", "adc", "--config", str(_config_path()), "--logtostderr"]
    oauth = os.getenv("COLAB_CLI_OAUTH_CONFIG", "").strip()
    if oauth:
        path = Path(oauth).expanduser().resolve()
        if path.is_file():
            argv[1:1] = ["-c", str(path)]
    return argv


_PROCS: dict[str, subprocess.Popen] = {}


def kill_task(task_id: str) -> None:
    proc = _PROCS.get(task_id)
    if proc and proc.poll() is None:
        _kill(proc)


def run_argv(argv: list[str], *, cwd: Path, timeout: float, task_id: str | None = None) -> CommandResult:
    """Run the fixed executable. argv must already be an allowlisted list."""
    if not argv or argv[0] != _executable():
        raise CliDisabled("CLI_ARGV", "拒绝执行未登记的程序")
    _trace(argv)
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if task_id:
        _PROCS[task_id] = proc
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode if proc.returncode is not None else 1
    except subprocess.TimeoutExpired:
        _kill(proc)
        out, err = proc.communicate()
        code = 124
        err = (err or "") + "\n[studio] CLI process exceeded the wall clock and was stopped."
    finally:
        if task_id:
            _PROCS.pop(task_id, None)
    text = redact((out or "") + ("\n" if out and err else "") + (err or ""))
    return CommandResult(argv=list(argv), code=code, text=text, auth_blocker=auth_blocker(text))


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()


def usage_view() -> dict:
    result = run_argv(_prefix() + ["usage"], cwd=root_dir(), timeout=30)
    if result.auth_blocker:
        return {"available": False, "reason": "auth", "auth_blocker": result.auth_blocker}
    if result.code != 0:
        return {"available": False, "reason": "unavailable", "detail": result.text[-500:]}
    balance = re.search(r"Current balance:\s*([0-9]+(?:\.[0-9]+)?)\s+compute units", result.text)
    rate = re.search(r"Usage rate:\s*([0-9]+(?:\.[0-9]+)?)/hr", result.text)
    assignments = re.search(r"Active assignments:\s*([0-9]+)", result.text)
    if not (balance and rate and assignments):
        return {"available": False, "reason": "unparsed"}
    return {
        "available": True,
        "source": "colab-cli",
        "balance": float(balance.group(1)),
        "rate_hourly": float(rate.group(1)),
        "assignments": int(assignments.group(1)),
    }


def new_session(name: str, accelerator: str, task_id: str) -> CommandResult:
    validate_session_name(name)
    argv = _prefix() + ["new", "-s", name]
    if accelerator != "cpu":
        if accelerator not in GPUS:
            raise CliDisabled("CLI_ACCELERATOR", "拒绝未允许的加速器")
        argv.extend(["--gpu", accelerator])
    return run_argv(argv, cwd=root_dir(), timeout=180, task_id=task_id)


def exec_script(name: str, script: Path, idle_seconds: int, task_id: str) -> CommandResult:
    validate_session_name(name)
    resolved = script.resolve()
    if not resolved.is_relative_to(root_dir().resolve()):
        raise CliDisabled("CLI_PATH", "脚本必须位于本任务目录")
    argv = _prefix() + ["exec", "-s", name, "-f", str(resolved), "--timeout", str(int(idle_seconds))]
    return run_argv(argv, cwd=script.parent, timeout=max(60, idle_seconds + 90), task_id=task_id)


def stop_session(name: str, task_id: str | None = None) -> CommandResult:
    validate_session_name(name)
    return run_argv(_prefix() + ["stop", "-s", name], cwd=root_dir(), timeout=120, task_id=task_id)


def list_sessions() -> CommandResult:
    return run_argv(_prefix() + ["sessions"], cwd=root_dir(), timeout=60)


def endpoint_for(name: str) -> str:
    """Read only the endpoint id. Tokens and URLs in the CLI state file stay on disk."""
    validate_session_name(name)
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    row = data.get(name) if isinstance(data, dict) else None
    endpoint = row.get("endpoint", "") if isinstance(row, dict) else ""
    if not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", endpoint):
        return ""
    return endpoint


def still_assigned(listing: str, name: str, endpoint: str) -> bool:
    if f"[{name}]" in listing:
        return True
    return bool(endpoint) and endpoint in listing


def release_proven(name: str, endpoint: str) -> tuple[bool, str]:
    stopped = stop_session(name)
    if stopped.auth_blocker:
        return False, stopped.auth_blocker
    listing = list_sessions()
    if listing.auth_blocker or listing.code != 0:
        return False, listing.text or "无法查询会话列表，不能证明运行时已释放"
    if still_assigned(listing.text, name, endpoint):
        return False, "colab sessions 仍能看到本任务的会话。CLI 的 Session terminated 不能当作释放成功。"
    return True, ""
