"""013 US2 — 자산 계보(asset_lineage) 조회. 읽기 전용·결정적(헌법 3조)·LLM 0.

기록(record_lineage)은 수집·관계 파이프라인이 이미 함. 본 모듈은 활동을 시간순으로 끌어올린다.
**도메인 제외 없음(2026-07-23 전면 제거)**: 의료 특수 트랙 미운용이라 도메인 무관 균일 노출.
자산은 status 무관 전부 포함(운영상 failed 계보 필요). 의료 복귀(3년차) 시 제외 재도입.
"""
from __future__ import annotations

from typing import Any

from service.portal._ext_expr import ext_expr  # 확장자 SQL 정규식 단일 출처(069 D4·057 FR-104)
from service.portal._timeline_util import TIMELINE_INTERVALS, pivot_series

# 057 FR-204: relations.proposed 판별 activity 는 054 스냅샷 카운트(asset_stats)와 **단일 출처** 공유 —
# 문자열 표류 방지(관계 제안 집계 두 곳이 같은 activity 를 본다).
from service.portal.asset_stats import _RELATION_PROPOSED_ACTIVITY

# 계보 조회 공통 FROM/JOIN(al=asset_lineage, a=asset). 동적 WHERE 절을 AND 로 이어 붙이기 위한
# always-true 앵커(WHERE TRUE)만 둔다. 도메인 제외 없음(2026-07-23 전면 제거·의료 특수 트랙 미운용).
_LINEAGE_FROM = (
    "FROM asset_lineage al JOIN asset a ON a.asset_id = al.asset_id "
    "WHERE TRUE"
)
# 파일 확장자(file_ext) = a.fs_path 마지막 .세그먼트(소문자). 단일 출처 ext_expr(별칭 a. — JOIN 모호성 차단).
_EXT_EXPR = ext_expr("a.")


def query_asset_lineage(conn: Any, asset_id: str, *, limit: int = 500) -> list[dict]:
    """자산의 활동을 발생 시각순으로 반환(도메인 제외 없음·2026-07-23) — [{activity, agent, used, generated, occurred_at}]."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT al.activity, al.agent, al.used, al.generated, al.occurred_at "
            + _LINEAGE_FROM + " AND al.asset_id = %s "
            "ORDER BY al.occurred_at ASC, al.lineage_id ASC LIMIT %s",
            (asset_id, limit))
        return [
            {"activity": act, "agent": ag, "used": used, "generated": gen,
             "occurred_at": ts.isoformat() if ts is not None else None}
            for act, ag, used, gen, ts in cur.fetchall()]


def query_lineage_feed(
    conn: Any, *, since: Any = None, until: Any = None, activity: str | None = None,
    modality: str | None = None, status: str | None = None, file_ext: str | None = None,
    limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """기간 내 전 자산 계보 피드(도메인 제외 없음·occurred_at DESC, lineage_id DESC·페이징·FR-009b).

    필터: 기간(since/until)·활동(activity)·**자산 차원**(modality·status·file_ext — asset 조인).
    대시보드 슬라이스용.
    """
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("al.occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("al.occurred_at < %s")
        params.append(until)
    if activity:
        conds.append("al.activity = %s")
        params.append(activity)
    if modality:
        conds.append("a.modality = %s")
        params.append(modality)
    if status:
        conds.append("a.status = %s")
        params.append(status)
    if file_ext:
        conds.append(f"{_EXT_EXPR} = %s")
        params.append(file_ext)
    extra = (" AND " + " AND ".join(conds)) if conds else ""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) " + _LINEAGE_FROM + extra, params)
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT al.lineage_id, al.asset_id, al.activity, al.agent, al.occurred_at "
            + _LINEAGE_FROM + extra
            + " ORDER BY al.occurred_at DESC, al.lineage_id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset])
        rows = [
            {"lineage_id": str(lid), "asset_id": str(aid), "activity": act, "agent": ag,
             "occurred_at": ts.isoformat() if ts is not None else None}
            for lid, aid, act, ag, ts in cur.fetchall()]
    # FR-701(054): 페이징 봉투 통일({rows,total,limit,offset}) — 프론트 목록 페이지/맨앞·맨끝 이동.
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


# 멀티시리즈 group_by 화이트리스트 → 컬럼식(고정 매핑·사용자 입력은 키로만 조회·인젝션 안전).
_GROUP_COLS = {"activity": "al.activity", "modality": "a.modality", "status": "a.status"}


def _lineage_filter(since: Any, until: Any, activity: str | None) -> tuple[str, list[Any]]:
    """계보 공통 필터 절(기간·활동) + 파라미터. _LINEAGE_FROM 뒤에 AND 로 붙인다."""
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("al.occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("al.occurred_at < %s")
        params.append(until)
    if activity:
        conds.append("al.activity = %s")
        params.append(activity)
    return ((" AND " + " AND ".join(conds)) if conds else ""), params


def lineage_stats(conn: Any, *, since: Any = None, until: Any = None,
                  activity: str | None = None) -> dict[str, Any]:
    """계보 집계(차트·KPI용·FR-009g 보완) — 총계 + 활동별·일별·modality·status·file_ext별. 결정적·도메인 제외 없음."""
    extra, params = _lineage_filter(since, until, activity)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) " + _LINEAGE_FROM + extra, params)
        total = int(cur.fetchone()[0])
        cur.execute("SELECT al.activity, COUNT(*) " + _LINEAGE_FROM + extra
                    + " GROUP BY al.activity ORDER BY COUNT(*) DESC, al.activity ASC", params)
        by_activity = [{"activity": a, "count": int(c)} for a, c in cur.fetchall()]
        cur.execute("SELECT al.occurred_at::date AS d, COUNT(*) " + _LINEAGE_FROM + extra
                    + " GROUP BY d ORDER BY d ASC", params)
        by_day = [{"day": d.isoformat() if d is not None else None, "count": int(c)}
                  for d, c in cur.fetchall()]
        cur.execute("SELECT a.modality, COUNT(*) " + _LINEAGE_FROM + extra
                    + " GROUP BY a.modality ORDER BY COUNT(*) DESC, a.modality ASC", params)
        by_modality = [{"modality": m, "count": int(c)} for m, c in cur.fetchall()]
        cur.execute("SELECT a.status, COUNT(*) " + _LINEAGE_FROM + extra
                    + " GROUP BY a.status ORDER BY COUNT(*) DESC, a.status ASC", params)
        by_status = [{"status": s, "count": int(c)} for s, c in cur.fetchall()]
        cur.execute(f"SELECT {_EXT_EXPR} AS ext, COUNT(*) " + _LINEAGE_FROM + extra
                    + " GROUP BY ext ORDER BY COUNT(*) DESC, ext ASC NULLS LAST", params)
        by_file_ext = [{"file_ext": e, "count": int(c)} for e, c in cur.fetchall()]
    return {"total": total, "by_activity": by_activity, "by_day": by_day,
            "by_modality": by_modality, "by_status": by_status, "by_file_ext": by_file_ext}


def lineage_timeline(conn: Any, *, since: Any = None, until: Any = None, activity: str | None = None,
                     interval: str = "day", group_by: str | None = None) -> dict[str, Any]:
    """계보 시계열(차트용·access timeline 과 대칭). group_by(activity/modality/status) 주면 멀티시리즈.

    결정적(시리즈 key ASC·버킷 ASC)·도메인 제외 없음. group_by 미지정이면 단일 시리즈({interval, buckets}).
    """
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
    extra, params = _lineage_filter(since, until, activity)
    with conn.cursor() as cur:
        if group_by in _GROUP_COLS:
            gcol = _GROUP_COLS[group_by]
            cur.execute(
                f"SELECT {gcol} AS key, date_trunc('{trunc}', al.occurred_at) AS bkt, COUNT(*) "
                + _LINEAGE_FROM + extra + " GROUP BY key, bkt ORDER BY key ASC, bkt ASC", params)
            return {"interval": trunc, "group_by": group_by, "series": pivot_series(cur.fetchall())}
        cur.execute(f"SELECT date_trunc('{trunc}', al.occurred_at) AS bkt, COUNT(*) "
                    + _LINEAGE_FROM + extra + " GROUP BY bkt ORDER BY bkt ASC", params)
        buckets = [{"bucket": b.isoformat() if b is not None else None, "count": int(c)}
                   for b, c in cur.fetchall()]
        return {"interval": trunc, "buckets": buckets}


def relation_proposed_summary(conn: Any, *, since: Any = None, until: Any = None,
                              interval: str = "day") -> dict[str, Any]:
    """관계 제안(relations.proposed.v1) distinct 자산 수 + 발생 추이(057 FR-204). 결정적·LLM 0·도메인 제외 없음.

    admin 관계-제안 화면이 ``getLineageFeed(limit:200)`` 원시 피드를 프론트에서 distinct/버킷팅하던 것을
    서버로 이관한다 — 200 초과 시 과소집계되던 **실버그**를 ``COUNT(DISTINCT al.asset_id)`` 전기간 집계로
    바로잡는다(LIMIT 캡 없음). 판별 activity 는 054 스냅샷 카운트(asset_stats)와 **단일 출처** 공유.

    - ``distinct_assets``: 관계 제안이 붙은 **고유 자산 수**(재실행 중복 제거).
    - ``timeline``: ``date_trunc(interval, occurred_at)`` 버킷별 **고유 자산 수**(bucket ASC·결정적).
      재실행 중복을 버킷 내에서 제거하므로 자산 추이가 부풀지 않는다. 자산이 서로 다른 날 제안되면 각
      버킷에 계수되어 sum(buckets) ≥ distinct_assets 일 수 있다(추이 관점·분할 아님).

    기간(since/until)은 **occurred_at**(제안 발생 시각) 기준·to exclusive. interval 은 TIMELINE_INTERVALS
    화이트리스트(f-string 안전·그 외 값은 'day' 폴백; API 계층이 422 로 선처리). SQL 등장 순서 =
    파라미터 순서(activity → occurred_since → occurred_until)로 순서 불변식을 지킨다.
    (도메인 제외 없음·2026-07-23 — ``_LINEAGE_FROM`` 은 ``WHERE TRUE`` 앵커만 둔다.)
    """
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
    where = _LINEAGE_FROM + " AND al.activity = %s"
    params: list[Any] = [_RELATION_PROPOSED_ACTIVITY]
    if since is not None:
        where += " AND al.occurred_at >= %s"
        params.append(since)
    if until is not None:
        where += " AND al.occurred_at < %s"
        params.append(until)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT al.asset_id) " + where, params)
        distinct_assets = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT date_trunc('{trunc}', al.occurred_at) AS bkt, COUNT(DISTINCT al.asset_id) "
            + where + " GROUP BY bkt ORDER BY bkt ASC", params)
        buckets = [{"bucket": b.isoformat() if b is not None else None, "count": int(c)}
                   for b, c in cur.fetchall()]
    return {"distinct_assets": distinct_assets,
            "timeline": {"interval": trunc, "buckets": buckets}}
