"""API 접근 이력 — 기록·조회·집계와 동작 이름 도출.

**흐름에서의 위치**: 미들웨어가 매 요청 끝에 여기로 한 행을 남기고, 관리자 화면이 그것을 읽는다.
기록은 **추가만** 한다 — 감사 자료라 수정·삭제 경로를 두지 않는다.
자산 데이터에는 손대지 않는다(헌법 6조) — 접근 이력 테이블에만 한 행씩 덧붙인다.
무엇을 어떤 동작으로 기록할지는 미들웨어가 경로를 보고 정한다.
"""
from __future__ import annotations

import json
import re
from typing import Any

from service.portal._timeline_util import TIMELINE_INTERVALS, pivot_series
from src.database.ids import uuid7

# /assets/{seg} 의 seg 가 자산 단건인지 판정하는 UUID 형식(대소문자 무관). 비-UUID(예약/컬렉션
# 세그먼트·오타)는 감사 대상에서 제외한다 — ``derive_access_action`` 설명 참조.
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
    """접근 이력 한 행을 남긴다.

    **DB에 쓴다**(추가만 — 수정·삭제 경로는 두지 않는다). 커밋은 호출자 몫이다.
    발생 시각은 앱이 아니라 **DB 시계**로 찍힌다 — 여러 워커의 시계가 어긋나도 순서가 뒤엉키지 않게.

    Args:
        action: 무슨 동작이었는지(``derive_access_action`` 이 만든 값).
        user_id: 요청 주체.
        asset_id: 대상 자산. 자산과 무관한 동작(검색 등)이면 ``None``.
            **UUID 가 아닌 값을 넣으면 INSERT 가 실패한다**(컬럼이 UUID 참조).
        detail: 부가 정보. ``None`` 이면 빈 객체로 저장한다.

    Returns:
        새로 만든 ``access_id``.
    """
    access_id = str(uuid7())
    with conn.cursor() as cur:
        cur.execute(_INSERT, (access_id, asset_id, user_id, action,
                              json.dumps(detail or {}, ensure_ascii=False)))
    return access_id


def derive_access_action(method: str, path: str) -> tuple[str, str | None] | None:
    """요청 경로를 보고 감사에 남길 동작 이름과 대상 자산을 정한다(순수 함수).

    ⚠ 데이터 라우트를 새로 추가하면 이 함수도 **동기 갱신**해야 감사가 기록된다(누락=조용히 미기록).
    ``/assets/`` 뒤 첫 세그먼트는 **UUID 형식일 때만** 자산 단건으로 간주한다 — ``/assets/unclassified``
    컬렉션처럼 UUID 가 아닌 경로 조각을 자산 id 로 오인하면 기록 INSERT 가
    매번 실패하고, 최선 노력 방식이라 조용히 삼켜진다(감사 유실 + 경고만 쌓임). 컬렉션 조회 기록이
    필요해지면 asset_id 없는 별도 action 으로 설계한다(현재는 단건·검색·다운로드·묶음만 감사).

    Args:
        method: HTTP 메서드. **GET 이 아니면 곧바로 기록 대상에서 뺀다**(조회만 감사한다).
        path: 요청 경로.

    Returns:
        ``(action, asset_id)``. 자산과 무관한 동작이면 asset_id 가 ``None`` 이고,
        **감사 대상이 아니면 전체가 ``None``**(호출부는 기록을 건너뛴다).
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
    """조건 목록을 WHERE 절로 조립한다.

    Args:
        conds: 이미 만들어진 조건 문자열들.

    Returns:
        ``" WHERE a AND b"`` 형태. **조건이 없으면 빈 문자열** — 절 자체를 붙이지 않는다.
    """
    return (" WHERE " + " AND ".join(conds)) if conds else ""


def query_access_logs(conn: Any, *, user_id: str | None = None, action: str | None = None,
                      since: Any = None, until: Any = None,
                      limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """접근 이력을 필터·페이징해 조회한다(조회 전용).

    Args:
        user_id: 사용자 필터. ``None`` 이면 전체.
        action: 동작 필터. ``None`` 이면 전체.
        since: 기간 시작(**포함**).
        until: 기간 끝(**미포함**) — 하루 단위로 끊을 때 경계가 겹치지 않게 한다.
        limit: 페이지 크기.
        offset: 건너뛸 행 수.

    Returns:
        ``{rows, total, limit, offset}``. ``total`` 은 **같은 필터**로 센 전체 건수다.
        정렬은 최신순이며, 같은 시각이면 id 순으로 갈라 순서를 고정한다.
    """
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
    # 페이징 응답 모양을 통일한다({rows,total,limit,offset}).
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def access_log_stats(conn: Any, *, since: Any = None, until: Any = None) -> dict[str, Any]:
    """총계와 동작별·사용자별 호출 수를 낸다(조회 전용).

    Args:
        since: 기간 시작(포함). ``None`` 이면 전체 기간.
        until: 기간 끝(미포함).

    Returns:
        ``{total, by_action, by_user}``. 각 목록은 많은 순 → 이름순으로 정렬해 순서를 고정한다.
    """
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
    """시간 버킷(일·시)별 호출 수를 낸다(시간순 고정).

    Args:
        since: 기간 시작(포함).
        until: 기간 끝(미포함).
        action: 특정 동작만 볼 때 지정. ``None`` 이면 전체.
        interval: 버킷 단위. **허용 목록 밖 값은 조용히 일 단위로 접는다** — 이 값은 SQL 에
            문자열로 박히므로 임의 값을 그대로 쓰면 안 된다.
        group_by: 시리즈를 가를 기준(``action``|``user_id``). 주면 응답이 **여러 시리즈**가 되고,
            주지 않으면 단일 시리즈다 — 응답 모양이 달라지므로 호출부가 분기해야 한다.

    Returns:
        단일: ``{interval, buckets}`` / 다중: ``{interval, group_by, series:[{key, buckets}]}``.
        버킷은 시간순으로 정렬된다.
    """
    # ⚠️ 버킷 단위는 아래 SQL 에 **문자열로 직접 박힌다** — 허용 목록을 통과한 값만 쓰고,
    # 그 밖이면 일 단위로 접는다(요청 값을 그대로 넣으면 안 된다).
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
        # 시리즈를 가르면 응답 모양 자체가 달라진다(단일 buckets → series 배열).
        if group_by in _TIMELINE_GROUP_COLS:
            # 컬럼명도 SQL 에 직접 박히므로 매핑을 통과한 값만 쓴다.
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
    """접근 이력 화면이 필요한 것을 **한 트랜잭션에서 한 번에** 만든다 — 기간 지표 + 추이.

    화면이 세 번 부르던 것을 한 번으로 묶은 응답이다. 계산은 이미 검증된 조회 함수 둘을
    그대로 재사용한다 — 여기서 다시 구현하지 않는다.

    Args:
        since: 기간 시작(포함).
        until: 기간 끝(미포함).
        action: 특정 동작으로 **추이만** 좁힌다. ⚠️ 지표(``total``·``by_action``)는 이 값과
            무관하게 **기간 전체**를 센다 — 드릴다운해도 전체 대비 비중을 볼 수 있어야 하기 때문이다.
        interval: 추이 버킷 단위. 허용 목록 밖이면 일 단위로 접힌다.

    Returns:
        ``{total, by_action, timeline}``. ``action`` 을 주면 추이는 단일 시리즈, 주지 않으면
        동작별 여러 시리즈다.
    """
    stats = access_log_stats(conn, since=since, until=until)
    timeline = access_log_timeline(
        conn, since=since, until=until, action=action, interval=interval, group_by="action")
    return {"total": stats["total"], "by_action": stats["by_action"], "timeline": timeline}
