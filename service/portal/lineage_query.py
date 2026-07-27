"""자산 계보 조회 — 어떤 처리가 언제 일어났는지(읽기 전용).

기록(record_lineage)은 수집·관계 파이프라인이 이미 함. 본 모듈은 활동을 시간순으로 끌어올린다.
**흐름에서의 위치**: 적재·관계 배치가 남긴 기록을 관리자 화면이 여기로 읽어 간다. 쓰기는 없다.
자산은 status 무관 전부 포함(운영상 failed 계보 필요). 의료 복귀(3년차) 시 제외 재도입.
"""
from __future__ import annotations

from typing import Any

from service.portal._ext_expr import ext_expr  # 확장자 추출 SQL 단일 출처(집계끼리 값이 맞아야 한다)
from service.portal._timeline_util import TIMELINE_INTERVALS, pivot_series

# '관계 제안됨'을 판별하는 활동 이름은 자산 집계 쪽과 **같은 상수**를 쓴다 —
# 문자열 표류 방지(관계 제안 집계 두 곳이 같은 activity 를 본다).
from service.portal.asset_stats import _RELATION_PROPOSED_ACTIVITY

# 계보 조회 공통 FROM/JOIN(al=asset_lineage, a=asset). 동적 WHERE 절을 AND 로 이어 붙이기 위한
# 조건이 없어도 뒤에 AND 를 이어 붙일 수 있도록 항상 참인 앵커만 둔다(도메인 제외는 없다).
_LINEAGE_FROM = (
    "FROM asset_lineage al JOIN asset a ON a.asset_id = al.asset_id "
    "WHERE TRUE"
)
# 파일 확장자(file_ext) = a.fs_path 마지막 .세그먼트(소문자). 단일 출처 ext_expr(별칭 a. — JOIN 모호성 차단).
_EXT_EXPR = ext_expr("a.")


def query_asset_lineage(conn: Any, asset_id: str, *, limit: int = 500) -> list[dict]:
    """한 자산의 처리 활동을 발생 시각순으로 돌려준다(조회 전용).

    Args:
        asset_id: 대상 자산.

    Returns:
        ``[{activity, agent, used, generated, occurred_at}]``. 활동이 없으면 빈 목록.
    """
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
    """기간 내 모든 자산의 계보를 최신순으로 페이징해 돌려준다(조회 전용).

    Args:
        conn: 열려 있는 연결.
        since: 발생 시각 하한(포함). ``None`` 이면 전체 기간.
        until: 발생 시각 상한(미포함).
        activity: 활동명 정확 일치.
        modality: 자산 모달리티. **활동이 아니라 자산 쪽 조건**이라 자산 테이블을 함께 읽는다.
        status: 자산 처리 단계(위와 같이 자산 쪽 조건).
        file_ext: 자산 확장자(위와 같이 자산 쪽 조건).
        limit: 한 페이지 행 수.
        offset: 건너뛸 행 수.

    Returns:
        행 목록과 총계를 담은 페이징 봉투.
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
    # 페이징 응답 모양을 통일한다({rows,total,limit,offset}) — total 이 있어야 화면이 맨끝으로 갈 수 있다.
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


# 멀티시리즈 group_by 화이트리스트 → 컬럼식(고정 매핑·사용자 입력은 키로만 조회·인젝션 안전).
_GROUP_COLS = {"activity": "al.activity", "modality": "a.modality", "status": "a.status"}


def _lineage_filter(since: Any, until: Any, activity: str | None) -> tuple[str, list[Any]]:
    """계보 조회들이 공유하는 필터 절을 만든다.

    Args:
        since: 기간 시작(포함).
        until: 기간 끝(미포함).
        activity: 활동 이름 필터. ``None`` 이면 전체.

    Returns:
        ``(AND 로 이어 붙일 절, 파라미터 목록)``. 조건이 없으면 절은 빈 문자열이다.
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
    return ((" AND " + " AND ".join(conds)) if conds else ""), params


def lineage_stats(conn: Any, *, since: Any = None, until: Any = None,
                  activity: str | None = None) -> dict[str, Any]:
    """계보를 여러 기준으로 집계한다 — 총계와 활동별·일별·모달리티·상태·확장자별.

    Args:
        since: 기간 시작(포함). ``None`` 이면 전체 기간.
        until: 기간 끝(미포함).

    Returns:
        집계 dict(차트가 그대로 쓸 형태). 각 목록은 정렬이 고정돼 있다.
    """
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
    """계보를 시간 버킷으로 묶어 추이를 낸다(차트용).

    Args:
        since: 기간 시작(포함).
        until: 기간 끝(미포함).
        activity: 활동 필터.
        interval: 버킷 단위. **허용 목록 밖이면 일 단위로 접는다**(SQL 에 문자열로 박히는 값).
        group_by: 시리즈를 가를 기준(활동·모달리티·상태). 주면 응답이 **여러 시리즈**가 된다.

    Returns:
        단일: ``{interval, buckets}`` / 다중: 시리즈 목록. 정렬은 고정된다.
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
    """관계가 제안된 자산 수와 그 추이를 낸다.

    세는 일을 화면에 맡기지 않는다 — 화면은 가져온 페이지 안에서만 셀 수 있어, 페이지를
    넘어가는 순간 실제보다 적게 나온다. 여기서는 상한 없이 전수로 센다. 무엇을 '관계 제안'
    으로 볼지는 자산 집계 쪽과 **같은 상수**를 쓴다.

    Args:
        since: 기간 시작(포함).
        until: 기간 끝(미포함).
        interval: 추이 버킷 단위.

    Returns:
        ``{distinct_assets, timeline}``.

    - ``distinct_assets``: 관계 제안이 붙은 **고유 자산 수** — 파이프라인을 다시 돌려
      같은 자산에 제안이 여러 번 붙어도 한 번만 센다.
    - ``timeline``: 버킷마다 고유 자산 수. ⚠️ **버킷 합계가 총계보다 클 수 있다** —
      한 자산이 서로 다른 날 제안되면 그 날짜마다 한 번씩 세기 때문이다(추이를 보는 값이지
      총계를 쪼갠 값이 아니다).
    """
    # ⚠️ 버킷 단위는 아래 SQL 에 **문자열로 직접 박힌다** — 허용 목록을 통과한 값만 쓴다.
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
    # 조건을 붙이는 순서 = 값을 넣는 순서다. 둘을 나란히 늘려 어긋날 여지를 없앤다.
    where = _LINEAGE_FROM + " AND al.activity = %s"
    params: list[Any] = [_RELATION_PROPOSED_ACTIVITY]
    if since is not None:
        where += " AND al.occurred_at >= %s"
        params.append(since)
    if until is not None:
        where += " AND al.occurred_at < %s"
        params.append(until)
    with conn.cursor() as cur:
        # 두 질의가 **같은 조건·같은 값**을 쓴다 — 다르면 총계와 추이가 서로 어긋난다.
        cur.execute("SELECT COUNT(DISTINCT al.asset_id) " + where, params)
        distinct_assets = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT date_trunc('{trunc}', al.occurred_at) AS bkt, COUNT(DISTINCT al.asset_id) "
            + where + " GROUP BY bkt ORDER BY bkt ASC", params)
        buckets = [{"bucket": b.isoformat() if b is not None else None, "count": int(c)}
                   for b, c in cur.fetchall()]
    return {"distinct_assets": distinct_assets,
            "timeline": {"interval": trunc, "buckets": buckets}}
