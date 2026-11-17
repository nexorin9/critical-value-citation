"""排班系统出站客户端（任务 id 11 · 三条真实出站通道之一）。

**对端**：院内排班 / 值班名单系统。

=====================  =====================================================
出站调用               用途
=====================  =====================================================
``GET {NURSE_ROSTER_BASE_URL}/api/v1/duty-roster?dept_code=…``
                     取某个科室当前当班人（**读**）
=====================  =====================================================

**凭据从环境变量读**：``NURSE_ROSTER_BASE_URL``（基地址）与 ``ROSTER_TOKEN``
（``Authorization: Bearer``）。代码内不出现密钥；**缺凭据位时在发请求之前
就报错** —— 不能「先发出去再说」，也不能拿本地替身顶上去。

**降级链**（排班系统不可用时的顺序）：

1. **实时读到** → 当班名单来源：排班系统实时（正常路径）；
2. **实时读不到，但缓存未过期** → 当班名单来源：缓存（取数时刻），
   卡片上标为**降级**，值班人知道这份名单有多旧；
3. **实时读不到，缓存也过期 / 从来没有** → **转人工**
   （``transferred_to_human``），由人接手找当班人 ——
   **不拿过期名单冒充当前当班人**。

注意第 3 条：**转人工是一个业务决定，不是异常兜底**。危急值宁可让人
打电话找当班人，也不能悄悄通知一个三小时前已经下班的人。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from critval.card.provenance import (
    ORIGIN_CACHE,
    ORIGIN_LIVE,
    ORIGIN_UNAVAILABLE,
    roster_provenance,
)
from critval.models import Provenance
from critval.roster.cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    CachedRoster,
    RosterCache,
)
from critval.roster.resolve import resolve_notify_target
from critval.roster.types import (
    DutyPerson,
    NotifyTarget,
    parse_roster_payload,
    pick_duty_person,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ROSTER_PATH",
    "RosterClient",
    "RosterError",
    "RosterResult",
]

#: 排班系统当班名单接口路径（拼在 ``NURSE_ROSTER_BASE_URL`` 之后）。
ROSTER_PATH = "/api/v1/duty-roster"

#: 默认超时（秒）。危急值等不起，对端慢也不能一直挂着。
DEFAULT_TIMEOUT_SECONDS = 5.0


class RosterError(RuntimeError):
    """排班系统出站调用失败。

    ``retryable`` 区分两类失败：``4xx`` 是**契约错**（路径不对、参数不对），
    重试没有意义；``5xx`` 与网络超时是**对端暂时不可用**，可以重试。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class RosterResult:
    """一次「取当班人」的结论。

    ``origin`` 说明**这份名单是怎么来的**（``live`` / ``cache`` /
    ``unavailable``）—— 卡片上的来源标注由它决定，值班人一眼能看出
    手里这份名单是不是权威实时值。
    """

    ok: bool
    people: tuple[DutyPerson, ...] = ()
    origin: str = ORIGIN_LIVE
    as_of: str = ""
    dept_code: str = ""
    error: str = ""
    status: int = 0
    retryable: bool = False
    #: 命中缓存时的缓存条目（审计用：能回答「这份名单什么时候取到的」）。
    cached: Optional[CachedRoster] = None

    @property
    def degraded(self) -> bool:
        """这份名单是不是降级来的（来自缓存）。"""
        return self.origin == ORIGIN_CACHE

    @property
    def requires_human(self) -> bool:
        """要不要转人工（排班系统不可用且没有可用缓存）。"""
        return self.origin == ORIGIN_UNAVAILABLE

    def provenance(self) -> Provenance:
        """这份名单的来源标注（装进处置卡的 ``provenance.duty_roster``）。"""
        if self.origin == ORIGIN_CACHE:
            return roster_provenance(ORIGIN_CACHE, as_of=self.as_of)
        if self.origin == ORIGIN_UNAVAILABLE:
            return roster_provenance(ORIGIN_UNAVAILABLE, detail=self.error)
        return roster_provenance(
            ORIGIN_LIVE,
            detail=self.duty_person(),
            as_of=self.as_of,
        )

    def duty_person(self) -> str:
        """通知对象的人话标签（空名单返回空串，不编）。

        **这是通道级的保守默认**（取第一条有姓名的）—— 真正的择人
        （班次窗口判定 + 角色优先级 + 值班长兜底）走
        :meth:`resolve`，别拿这个当通知对象。
        """
        person = pick_duty_person(self.people)
        return person.label() if person else ""

    def resolve(
        self,
        *,
        at: Optional[datetime] = None,
        cache_ttl_seconds: Optional[float] = None,
    ) -> NotifyTarget:
        """把这次取数结果解析成**唯一**的通知对象（任务 id 41）。

        通道只负责「读得到」，择人是另一件事：按观察时刻落在哪个班次
        窗口选人（夜班跨零点），同科室多人在班时按角色优先级唯一化，
        无人在班时走科室值班长兜底，缓存过期则转人工。
        """
        return resolve_notify_target(
            self.people,
            dept_code=self.dept_code,
            at=at,
            origin=self.origin,
            as_of=self.as_of,
            cache_entry=self.cached,
            cache_ttl_seconds=(
                DEFAULT_CACHE_TTL_SECONDS
                if cache_ttl_seconds is None
                else cache_ttl_seconds
            ),
            note=self.error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "origin": self.origin,
            "as_of": self.as_of,
            "dept_code": self.dept_code,
            "error": self.error,
            "status": self.status,
            "retryable": self.retryable,
            "degraded": self.degraded,
            "requires_human": self.requires_human,
            "people": [person.to_dict() for person in self.people],
        }


class RosterClient:
    """排班系统 REST 客户端（读当班人）。

    * 基地址：``NURSE_ROSTER_BASE_URL``（环境变量）；
    * 凭据：``ROSTER_TOKEN``（环境变量），``Authorization: Bearer``；
    * 出错分级：``4xx`` 不可重试（契约错），``5xx`` / 超时可重试。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        path: str = ROSTER_PATH,
        opener: Optional[Any] = None,
        cache: Optional[RosterCache] = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.path = path if path.startswith("/") else f"/{path}"
        self._opener = opener or urllib.request.urlopen
        self.cache = cache if cache is not None else RosterCache()
        self.cache_ttl_seconds = float(cache_ttl_seconds)

    # ---- 构造 -------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        cache: Optional[RosterCache] = None,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    ) -> "RosterClient":
        """按环境变量装配客户端；缺凭据位**立即报错**（不发起任何请求）。"""
        from critval.config import Config

        base_url = Config.get_env("NURSE_ROSTER_BASE_URL") or ""
        token = Config.get_env("ROSTER_TOKEN") or ""
        if not base_url:
            raise RosterError(
                "缺少 NURSE_ROSTER_BASE_URL（排班系统根地址），无法发起出站调用"
            )
        if not token:
            raise RosterError(
                "缺少 ROSTER_TOKEN（排班系统 Bearer 凭据），无法发起出站调用"
            )
        return cls(
            base_url,
            token,
            timeout=timeout,
            cache=cache,
            cache_ttl_seconds=cache_ttl_seconds,
        )

    @property
    def endpoint(self) -> str:
        """完整目标地址（不含 query）。"""
        return f"{self.base_url}{self.path}"

    # ---- 出站 -------------------------------------------------------------
    def fetch(self, dept_code: str, *, at: Optional[datetime] = None) -> RosterResult:
        """取某个科室的当班人。

        返回值永远是 :class:`RosterResult` —— 读不到不算异常（那是业务里
        天天发生的事），而是通过 ``origin`` 把「实时 / 缓存 / 不可用」
        三种情形分开告诉调用方。
        """
        dept = str(dept_code or "").strip()
        if not self.base_url:
            raise RosterError("缺少 NURSE_ROSTER_BASE_URL，无法发起出站调用")
        if not self.token:
            raise RosterError("缺少 ROSTER_TOKEN，无法发起出站调用")

        live = self._request(dept)
        if live.ok:
            people = live.people
            moment = at or datetime.now()
            try:
                self.cache.put(dept, people, at=moment)
            except Exception:  # noqa: BLE001 —— 缓存写失败不该拖垮取名单
                pass
            return live

        # 实时读不到 → 降级到缓存（未过期才用）。
        cached = self.cache.get(dept)
        if cached is not None and not cached.expired(self.cache_ttl_seconds):
            return RosterResult(
                ok=True,
                people=cached.people,
                origin=ORIGIN_CACHE,
                as_of=cached.as_of_text(),
                dept_code=dept,
                error=live.error,
                status=live.status,
                retryable=live.retryable,
                cached=cached,
            )

        reason = live.error or "排班系统不可用"
        if cached is not None:
            reason = f"{reason}；缓存已过期（取数时刻 {cached.as_of_text()}）"
        return RosterResult(
            ok=False,
            people=(),
            origin=ORIGIN_UNAVAILABLE,
            as_of="",
            dept_code=dept,
            error=reason,
            status=live.status,
            retryable=live.retryable,
        )

    # ---- 择人 -------------------------------------------------------------
    def resolve(
        self,
        dept_code: str,
        *,
        at: Optional[datetime] = None,
    ) -> NotifyTarget:
        """取当班人并直接解析成**唯一**的通知对象（任务 id 41）。

        这是处置卡装配前的**一条龙出口**：读名单 → 班次窗口判定 →
        角色优先级唯一化 → 值班长兜底 → 缓存过期转人工，全在一处。

        >>> target = client.resolve("ICU", at=datetime(2026, 10, 29, 21, 20))
        >>> target.label()          # 通知对象（唯一）
        '李静 · 检验科值班技师 · 工号 10231'
        >>> target.reason           # 机器可解析的原因码
        'resolved'
        """
        result = self.fetch(dept_code, at=at)
        return result.resolve(at=at, cache_ttl_seconds=self.cache_ttl_seconds)

    # ---- 内部 -------------------------------------------------------------
    def _request(self, dept_code: str) -> RosterResult:
        query = urllib.parse.urlencode({"dept_code": dept_code})
        url = f"{self.endpoint}?{query}"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            return RosterResult(
                ok=False,
                origin=ORIGIN_UNAVAILABLE,
                dept_code=dept_code,
                error=f"排班系统返回 {exc.code}",
                status=exc.code,
                retryable=exc.code >= 500 or exc.code in {408, 429},
            )
        except urllib.error.URLError as exc:
            return RosterResult(
                ok=False,
                origin=ORIGIN_UNAVAILABLE,
                dept_code=dept_code,
                error=f"排班系统不可达：{exc.reason}",
                status=0,
                retryable=True,
            )
        except TimeoutError as exc:
            return RosterResult(
                ok=False,
                origin=ORIGIN_UNAVAILABLE,
                dept_code=dept_code,
                error=f"排班系统调用超时（{self.timeout} 秒）：{exc}",
                status=0,
                retryable=True,
            )

        if not (200 <= status < 300):
            return RosterResult(
                ok=False,
                origin=ORIGIN_UNAVAILABLE,
                dept_code=dept_code,
                error=f"排班系统返回 {status}",
                status=status,
                retryable=status >= 500 or status in {408, 429},
            )

        payload: Any = None
        if text.strip():
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                return RosterResult(
                    ok=False,
                    origin=ORIGIN_UNAVAILABLE,
                    dept_code=dept_code,
                    error=f"排班系统响应不是合法 JSON：{exc.msg}",
                    status=status,
                )
        people = parse_roster_payload(payload)
        return RosterResult(
            ok=True,
            people=people,
            origin=ORIGIN_LIVE,
            as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
            dept_code=dept_code,
            status=status,
        )
