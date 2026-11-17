"""排班系统出站通道单测（任务 id 11）。

盯住四件事：

* **真协议真凭据** —— 请求真的发出去了，头里真的带 Bearer；
* **降级链不跳级** —— 实时读到 → 缓存（带时间戳）→ 转人工，顺序不能乱；
* **过期缓存不作数** —— 缓存超期就不拿它当通知依据，事件转人工；
* **不编当班人** —— 读不到人时通知对象是缺项，不是随便挑一个。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from critval.card.provenance import ORIGIN_CACHE, ORIGIN_LIVE, ORIGIN_UNAVAILABLE
from critval.roster.cache import RosterCache
from critval.roster.client import RosterClient, RosterError
from critval.roster.types import parse_roster_payload

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures.roster_double import RosterDouble  # noqa: E402


TOKEN = "test-roster-token-0001"
DEPT = "ICU"


@pytest.fixture()
def double() -> RosterDouble:
    instance = RosterDouble(TOKEN)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture()
def cache(tmp_path: Path) -> RosterCache:
    return RosterCache(tmp_path / "roster_cache.json", ttl_seconds=900)


def _client(double: RosterDouble, cache: RosterCache) -> RosterClient:
    return RosterClient(double.base_url, TOKEN, cache=cache)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_实时读到当班人且请求带Bearer(double: RosterDouble, cache: RosterCache):
    double.seed(
        DEPT,
        [
            {
                "姓名": "李静",
                "工号": "10231",
                "岗位": "检验科值班技师",
                "shift_start": "08:00",
                "shift_end": "20:00",
            }
        ],
    )
    client = _client(double, cache)
    result = client.fetch(DEPT)

    assert result.ok is True
    assert result.origin == ORIGIN_LIVE
    assert result.degraded is False
    assert result.requires_human is False
    assert len(result.people) == 1
    assert result.people[0].name == "李静"
    assert result.people[0].staff_id == "10231"
    # 请求确实发出去了，且带 Bearer 头与 dept_code 查询参数。
    assert double.call_count() == 1
    method, path, auth = double.calls[0]
    assert method == "GET"
    assert path.startswith("/api/v1/duty-roster?")
    assert "dept_code=ICU" in path
    assert auth == f"Bearer {TOKEN}"


def test_通知对象标签含姓名岗位与工号(double: RosterDouble, cache: RosterCache):
    double.seed(
        DEPT,
        [{"姓名": "李静", "岗位": "值班技师", "工号": "10231"}],
    )
    result = _client(double, cache).fetch(DEPT)
    label = result.duty_person()
    assert "李静" in label
    assert "值班技师" in label
    assert "10231" in label


def test_读到即写缓存并带取数时刻(double: RosterDouble, cache: RosterCache):
    double.seed(DEPT, [{"姓名": "李静"}])
    client = _client(double, cache)
    client.fetch(DEPT)

    entry = cache.get(DEPT)
    assert entry is not None
    assert len(entry.people) == 1
    assert entry.fetched_at.tzinfo is not None
    assert entry.as_of_text()


def test_缺凭据位时在发请求之前就报错(double: RosterDouble):
    with pytest.raises(RosterError) as excinfo:
        RosterClient("", TOKEN).fetch(DEPT)
    assert "NURSE_ROSTER_BASE_URL" in str(excinfo.value)

    with pytest.raises(RosterError) as excinfo:
        RosterClient(double.base_url, "").fetch(DEPT)
    assert "ROSTER_TOKEN" in str(excinfo.value)
    # 一次请求都不该发出去。
    assert double.call_count() == 0


# ---------------------------------------------------------------------------
# 降级链
# ---------------------------------------------------------------------------


def test_排班系统不可用时降级为带时间戳的缓存(double: RosterDouble, cache: RosterCache):
    double.seed(DEPT, [{"姓名": "李静", "岗位": "值班技师"}])
    client = _client(double, cache)
    client.fetch(DEPT)  # 先成功一次，缓存里有名单

    double.status_code = 503
    result = client.fetch(DEPT)

    assert result.ok is True
    assert result.origin == ORIGIN_CACHE
    assert result.degraded is True
    assert result.requires_human is False
    assert result.people[0].name == "李静"
    # 缓存来源标注必须带取数时刻。
    assert result.as_of
    assert "缓存" in result.provenance().origin or result.provenance().origin == ORIGIN_CACHE
    assert result.provenance().as_of == result.as_of


def test_缓存过期后转人工而不是拿旧名单冒充(double: RosterDouble, cache: RosterCache):
    double.seed(DEPT, [{"姓名": "李静"}])
    client = _client(double, cache)
    client.fetch(DEPT)

    # 把缓存的取数时刻推到 TTL 之外。
    stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
    cache.put(DEPT, cache.get(DEPT).people, at=stale)

    double.status_code = 503
    result = client.fetch(DEPT)

    assert result.ok is False
    assert result.origin == ORIGIN_UNAVAILABLE
    assert result.requires_human is True
    assert result.people == ()
    assert "过期" in result.error


def test_从来没有缓存且对端挂掉时转人工(double: RosterDouble, cache: RosterCache):
    double.status_code = 500
    result = _client(double, cache).fetch(DEPT)

    assert result.ok is False
    assert result.origin == ORIGIN_UNAVAILABLE
    assert result.requires_human is True
    assert result.duty_person() == ""


def test_对端401时降级到缓存但错误里说清是凭据问题(double: RosterDouble, cache: RosterCache):
    double.seed(DEPT, [{"姓名": "李静"}])
    client = _client(double, cache)
    client.fetch(DEPT)

    wrong = RosterClient(double.base_url, "wrong-token-0001", cache=cache)
    result = wrong.fetch(DEPT)

    # 读不到就降级到带时间戳的缓存（名单还在，来源标清楚）。
    assert result.ok is True
    assert result.origin == ORIGIN_CACHE
    assert result.degraded is True
    # 但错误要说清是 401 —— 运维得知道该换凭据。
    assert "401" in result.error


def test_对端401且没有缓存时转人工(double: RosterDouble, cache: RosterCache):
    wrong = RosterClient(double.base_url, "wrong-token-0001", cache=cache)
    result = wrong.fetch(DEPT)

    assert result.ok is False
    assert result.origin == ORIGIN_UNAVAILABLE
    assert result.requires_human is True
    assert "401" in result.error


def test_过期条目仍留在缓存里可被审计(tmp_path: Path):
    cache = RosterCache(tmp_path / "roster_cache.json", ttl_seconds=1)
    cache.put(DEPT, [], at=datetime.now(timezone.utc) - timedelta(seconds=60))
    expired = cache.expired_entries()
    assert len(expired) == 1
    assert expired[0].dept_code == DEPT
    # 没被静默删掉。
    assert cache.get(DEPT) is not None


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def test_响应外壳换了也读得出人():
    for payload in (
        [{"姓名": "李静"}],
        {"data": [{"姓名": "李静"}]},
        {"duty": [{"姓名": "李静"}]},
        {"roster": [{"姓名": "李静"}]},
        {"persons": [{"姓名": "李静"}]},
    ):
        people = parse_roster_payload(payload)
        assert len(people) == 1, payload
        assert people[0].name == "李静"


def test_英文键与中文键都能读():
    people = parse_roster_payload(
        {"data": [{"name": "王敏", "staff_id": "A01", "role": "护士"}]}
    )
    assert people[0].name == "王敏"
    assert people[0].staff_id == "A01"
    assert people[0].role == "护士"


def test_读不出人不算有人():
    assert parse_roster_payload({"data": []}) == ()
    assert parse_roster_payload({"data": [{"备注": "无"}]}) == ()
    assert parse_roster_payload({"unexpected": "shape"}) == ()


def test_未知字段原样留在extra里():
    people = parse_roster_payload({"姓名": "李静", "手机分机": "8021"})
    assert people[0].extra.get("手机分机") == "8021"


def test_响应不是合法JSON时转人工(double: RosterDouble, cache: RosterCache):
    double.raw_body = "not-json"
    result = _client(double, cache).fetch(DEPT)
    assert result.ok is False
    assert result.requires_human is True
    assert "JSON" in result.error
