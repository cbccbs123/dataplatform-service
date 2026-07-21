"""013 US3 — API 접근 이력(access_log) 기록·조회·집계 + 접근 action 도출.

기록은 append-only 감사 write(013 FR-012 감사 무결성). 조회·집계는 읽기 전용·결정적(헌법 3조)·LLM 0.
자산 데이터/스키마는 무변경(헌법 6조) — access_log 만 append-only 로 적재한다.
portal_api 미들웨어가 derive_access_action 으로 (action, asset_id)를 정해 record_access 로 적재한다.
"""
from __future__ import annotations

import json
import re
from typing import Any

from src.database.ids import uuid7
from service.portal._timeline_util import TIMELINE_INTERVALS, pivot_series

# /assets/{seg} 의 seg 가 자산 단건인지 판정하는 UUID 형식(대소문자 무관). 비-UUID(예약/컬렉션
# 세그먼트·오타)는 감사 대상에서 제외한다 — derive_access_action docstring 참조(2026-07-15 B3).
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"  # \Z: $ 는 말미 개행 허용
)

_INSERT = (
    "INSERT INTO access_log (access_id, asset_id, user_id, action, detail) "
    "VALUES (%s, %s, %s, %s, %s::jsonb)"
)
_COLS = "access_id, action, user_id, asset_id, occurred_at"


def record_access(conn: Any, *, action: str, user_id: str,
                  asset_id: str | None = None, detail: dict | None = None) -> str:
    """access_log 한 행 INSERT(append-only). access_id(uuid7) 반환·occurred_at 은 DB now()."""
    access_id = str(uuid7())
    with conn.cursor() as cur:
        cur.execute(_INSERT, (access_id, asset_id, user_id, action,
                              json.dumps(detail or {}, ensure_ascii=False)))
    return access_id


def derive_access_action(method: str, path: str) -> tuple[str, str | None] | None:
    """데이터 접근 GET 라우트 → (action, asset_id). 그 외(감사뷰·비GET·health 등)는 None(기록 안 함).

    ⚠ 데이터 라우트를 새로 추가하면 이 함수도 **동기 갱신**해야 감사가 기록된다(누락=조용히 미기록).
    ``/assets/`` 뒤 첫 세그먼트는 **UUID 형식일 때만** 자산 단건으로 간주한다 — ``/assets/unclassified``
    (070 컬렉션) 같은 비-UUID 세그먼트를 asset_id 로 오인하면 ``access_log.asset_id``(UUID FK) INSERT 가
    매번 실패해 best-effort 로 삼켜졌다(감사 유실+경고 노이즈 — 2026-07-15 리뷰 B3). 컬렉션 조회 감사가
    필요해지면 asset_id 없는 별도 action 으로 설계한다(현재는 단건·검색·다운로드·bundle 만 감사).
    """
    if method.upper() != "GET":
        return None
    p = path.rstrip("/")
    if p == "/search":
        return ("search", None)
    if p.startswith("/assets/"):
        parts = p[len("/assets/"):].split("/")
        asset_id = parts[0]
        if not asset_id or not _UUID_RE.match(asset_id):
            return None  # 비-UUID(unclassified 등 컬렉션/예약 세그먼트) — 단건 감사 아님(B3)
        if len(parts) == 1:
            return ("asset_view", asset_id)
        if len(parts) == 2 and parts[1] == "download":
            return ("download", asset_id)
        if len(parts) == 2 and parts[1] == "bundle":
            return ("bundle", asset_id)
    return None


def _filter_clause(conds: list[str]) -> str:
    return (" WHERE " + " AND ".join(conds)) if conds else ""


def query_access_logs(conn: Any, *, user_id: str | None = None, action: str | None = None,
                      since: Any = None, until: Any = None,
                      limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """필터(사용자·action·기간)·페이징 조회. occurred_at DESC, access_id DESC tiebreak(결정적)."""
    conds: list[str] = []
    params: list[Any] = []
    if user_id:
        conds.append("user_id = %s")
        params.append(user_id)
    if action:
        conds.append("action = %s")
        params.append(action)
    if since is not None:
        conds.append("occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("occurred_at < %s")
        params.append(until)
    clause = _filter_clause(conds)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM access_log" + clause, params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT {_COLS} FROM access_log{clause} "
            "ORDER BY occurred_at DESC, access_id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset])
        rows = [
            {"access_id": str(a), "action": act, "user_id": u,
             "asset_id": str(aid) if aid is not None else None,
             "occurred_at": ts.isoformat() if ts is not None else None}
            for a, act, u, aid, ts in cur.fetchall()]
    # FR-701(054): 페이징 봉투 통일({rows,total,limit,offset}) — 프론트 목록 페이지/맨앞·맨끝 이동.
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def access_log_stats(conn: Any, *, since: Any = None, until: Any = None) -> dict[str, Any]:
    """기본 집계: 총계·action별·user별 호출 수(count DESC, key ASC tiebreak·결정적·FR-009a)."""
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("occurred_at < %s")
        params.append(until)
    clause = _filter_clause(conds)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM access_log" + clause, params)
        total = int(cur.fetchone()[0])
        cur.execute(f"SELECT action, COUNT(*) FROM access_log{clause} "
                    "GROUP BY action ORDER BY COUNT(*) DESC, action ASC", params)
        by_action = [{"action": a, "count": int(c)} for a, c in cur.fetchall()]
        cur.execute(f"SELECT user_id, COUNT(*) FROM access_log{clause} "
                    "GROUP BY user_id ORDER BY COUNT(*) DESC, user_id ASC", params)
        by_user = [{"user_id": u, "count": int(c)} for u, c in cur.fetchall()]
    return {"total": total, "by_action": by_action, "by_user": by_user}


# group_by 멀티시리즈 화이트리스트 → 컬럼식(고정 매핑·인젝션 안전). action/user_id 만 허용.
_TIMELINE_GROUP_COLS = {"action": "action", "user_id": "user_id"}


def access_log_timeline(conn: Any, *, since: Any = None, until: Any = None, action: str | None = None,
                        interval: str = "day", group_by: str | None = None) -> dict[str, Any]:
    """시계열 타임라인: 버킷(day/hour)별 호출 수(bucket ASC·결정적·FR-009c).

    ``group_by``(action/user_id) 주면 **멀티시리즈**({interval, group_by, series:[{key, buckets}]}),
    미지정이면 단일 시리즈({interval, buckets})·``action`` 필터=단일 api. trunc 화이트리스트(f-string 안전)·
    그 외 값은 %s 바인딩.
    """
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("occurred_at < %s")
        params.append(until)
    if action:
        conds.append("action = %s")
        params.append(action)
    clause = (" WHERE " + " AND ".join(conds)) if conds else ""
    with conn.cursor() as cur:
        if group_by in _TIMELINE_GROUP_COLS:
            gcol = _TIMELINE_GROUP_COLS[group_by]
            cur.execute(
                f"SELECT {gcol} AS key, date_trunc('{trunc}', occurred_at) AS bkt, COUNT(*) "
                f"FROM access_log{clause} GROUP BY key, bkt ORDER BY key ASC, bkt ASC", params)
            return {"interval": trunc, "group_by": group_by, "series": pivot_series(cur.fetchall())}
        cur.execute(
            f"SELECT date_trunc('{trunc}', occurred_at) AS bkt, COUNT(*) FROM access_log{clause} "
            "GROUP BY bkt ORDER BY bkt ASC", params)
        buckets = [
            {"bucket": b.isoformat() if b is not None else None, "count": int(c)}
            for b, c in cur.fetchall()]
    return {"interval": trunc, "buckets": buckets}


def access_log_overview(conn: Any, *, since: Any = None, until: Any = None,
                        action: str | None = None, interval: str = "day") -> dict[str, Any]:
    """접근 이력 overview BFF(057 FR-301) — 기간 KPI(총계·action별) + 추이를 **한 트랜잭션·1회 응답**.

    프론트가 stats+list+timeline 3회 순차 호출하던 것을 stats+timeline **1회**로 묶는다(list 는 별도
    페이징 유지). 검증된 순수 조회 함수 2종(``access_log_stats``·``access_log_timeline``)을 그대로
    재사용해 재구현 0·결정성·LLM 0(``build_dashboard_summary`` 조합 패턴을 따른다).

    - ``total``·``by_action``: 기간 전체 KPI(``access_log_stats`` — action 무필터·전 action 분포).
    - ``timeline``: ``group_by="action"`` 멀티시리즈. ``action`` 지정 시 그 action 으로 **드릴다운**
      (해당 action 단일 시리즈)한다 — action 은 추이만 스코프하고 KPI(total/by_action)는 기간 전체다.

    ``interval`` 은 ``access_log_timeline`` 화이트리스트(day/hour/month·그 외 day 폴백; API 계층 422).
    """
    stats = access_log_stats(conn, since=since, until=until)
    timeline = access_log_timeline(
        conn, since=since, until=until, action=action, interval=interval, group_by="action")
    return {"total": stats["total"], "by_action": stats["by_action"], "timeline": timeline}
