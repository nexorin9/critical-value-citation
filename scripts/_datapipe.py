"""数据管道共享工具。

三个脚本 fetch_data.py / gen_synth.py / build_materials.py 共用本模块：
路径常量、sha256 校验、清单读写、带超时与重试的 HTTP 抓取、断网降级开关。

原则：任何网络异常都不得抛栈退出；抓取失败只记录原因并走降级路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 强制离线开关：设置后所有抓取立即失败并走降级路径（测试与院内离线部署共用）
OFFLINE_ENV = "CRITVAL_OFFLINE"

USER_AGENT = "critval-data-pipeline/0.1 (hospital critical-value demo pipeline)"

# 抓取超时（秒）。院内网 / 弱网环境下不宜过长，避免一轮任务被单个源拖死。
FETCH_TIMEOUT = 25
FETCH_RETRIES = 2


def offline_requested() -> bool:
    """是否被环境变量强制离线。"""
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, default=None):
    """读 JSON；文件缺失或损坏时返回 default（不抛栈）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class Source:
    """一个公开数据源。url / license / license_url 三件套必须完整，进 manifest。"""

    key: str
    url: str
    filename: str
    license: str
    license_url: str
    kind: str
    note: str = ""

    def to_record(self, dest: Path, ok: bool, reason: str) -> dict:
        record = {
            "key": self.key,
            "url": self.url,
            "license": self.license,
            "license_url": self.license_url,
            "kind": self.kind,
            "local_path": str(dest),
            "reachable": ok,
            "source": "downloaded" if ok else "unreachable",
            "degradation_reason": reason,
        }
        if ok and dest.exists():
            record["sha256"] = sha256_file(dest)
            record["bytes"] = dest.stat().st_size
        return record


def download(source: Source, dest: Path, timeout: int = FETCH_TIMEOUT, retries: int = FETCH_RETRIES):
    """抓取单个来源，返回 (是否成功, 失败原因)。任何异常都转成失败原因，不抛栈。"""
    if offline_requested():
        return False, f"offline_forced_by_env:{OFFLINE_ENV}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    last_reason = "unknown_error"

    for attempt in range(max(1, retries)):
        try:
            request = urllib.request.Request(source.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    last_reason = f"http_status_{status}"
                    continue
                payload = response.read()
            if not payload:
                last_reason = "empty_payload"
                continue
            dest.write_bytes(payload)
            return True, ""
        except urllib.error.HTTPError as exc:
            last_reason = f"http_error_{exc.code}"
        except urllib.error.URLError as exc:
            last_reason = "url_error_{}".format(getattr(exc, "reason", exc))
        except TimeoutError:
            last_reason = "timeout"
        except OSError as exc:
            last_reason = "io_error_{}".format(type(exc).__name__)
    return False, last_reason


def verify_sha256(path: Path, expected: str) -> bool:
    """校验本地文件与记录的 sha256 是否一致。"""
    if not expected or not path.exists():
        return False
    return sha256_file(path) == expected
