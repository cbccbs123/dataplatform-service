"""자산 집계·목록 조회 — 대시보드 숫자와 목록 화면이 쓰는 읽기 전용 계층.

**흐름에서의 위치**: 관리자 라우트가 이 함수들을 부른다. SELECT 만 하며 쓰기는 없다.

**정렬에 2차 키를 반드시 둔다**(개수 같으면 이름순, 시각 같으면 id 순) — 없으면 같은 요청이
매번 다른 순서를 내놓아 화면이 흔들리고 페이징이 어긋난다.
"""
from __future__ import annotations

from typing import Any

from service.portal._ext_expr import (
    ext_expr,  # 확장자 추출 SQL 단일 출처(집계·목록이 같은 식을 써야 값이 맞는다)
)
from service.portal._timeline_util import TIMELINE_INTERVALS, pivot_series
from src.config.filename_util import (
    display_file_name,  # 화면용 파일명 — 저장 시 붙은 id 접두를 떼어 준다
)

# 도메인별 제외는 없다 — 통계·목록 모두 전 도메인을 균일하게 센다.
# 파일 확장자(file_ext) = fs_path 마지막 .세그먼트(소문자·없으면 NULL). 단일 출처 ext_expr(비한정 fs_path).
_EXT_EXPR = ext_expr()

# 운영 관점 5버킷 — 세분화된 처리 상태를 화면이 이해할 단위로 묶는다.
# 버킷 순서 = 응답/집계 열거 순서(결정적). relation_proposed 는 registered 중 관계 제안이 있는 하위집합.
_SNAPSHOT_BUCKETS = ("processing", "deferred", "registered", "failed", "relation_proposed")
# '진행 중'에 해당하는 상태 4종. ⚠️ classified 는 여기 넣지 않는다 — 그것은 처리 단계가
# 아니라 분류 결과 표식이라, 넣으면 이미 끝난 자산이 진행 중으로 잡힌다.
_PROCESSING_STATUSES = ("received", "routing", "classifying", "extracting")
# relation_proposed 판별용 계보 activity(자산에 관계 제안이 붙은 lineage 기록).
_RELATION_PROPOSED_ACTIVITY = "relations.proposed.v1"
_RELATION_SCOPES = ("period", "alltime")  # 값 검증은 API 계층 몫 — 여기서는 쓰기만 한다
# 자산 생성 추이 group_by 화이트리스트 → 컬럼식(고정 매핑·사용자 입력은 키로만 조회·인젝션 안전).
# file_ext 는 평컬럼이 아닌 확장자 정규식(_EXT_EXPR) — 단일 테이블(asset) 쿼리라 fs_path 비한정 안전.
_GROUP_COLS = {"modality": "modality", "status": "status", "domain": "domain_label",
               "file_ext": _EXT_EXPR}


def _relation_proposed_exists(pfx: str, *, scoped: bool) -> str:
    """asset_lineage 에 relation 제안 기록이 있는지 검사하는 EXISTS 조각.

    서브쿼리 쪽은 자체 별칭을 쓰므로 바깥 테이블과 컬럼이 겹치지 않는다 — 접두사를 신경 쓸
    곳은 바깥 asset 컬럼뿐이다.

    Args:
        pfx: 바깥 asset 테이블 접두사. 조인 경로에서는 ``"a."``, 단일 테이블이면 빈 문자열 —
            **틀리면 컬럼이 모호해져 쿼리가 실패한다**.
        scoped: 관계 제안 시점을 기간으로 좁힐지. 켜면 파라미터 자리가 **2개 늘어난다**.

    Returns:
        EXISTS SQL 조각. 파라미터 순서는 activity → (좁힐 때) 시작 → 끝이며, 호출부는 이
        순서대로 값을 넣어야 한다.
    """
    occ = " AND l.occurred_at >= %s AND l.occurred_at < %s" if scoped else ""
    return (f"EXISTS (SELECT 1 FROM asset_lineage l WHERE l.asset_id = {pfx}asset_id "
            f"AND l.activity = %s{occ})")


def _snapshot_bucket_predicate(bucket: str, pfx: str, *, relation_scope: str,
                               since: Any, until: Any) -> tuple[str, list[Any]]:
    """스냅샷 버킷 → (WHERE 조각, 파라미터 list). status 집합 + relation_proposed EXISTS 분기.

    ``pfx`` = asset 테이블 접두("" / "a."). relation_scope='period' 이고 since/until 이 둘 다 있으면
    EXISTS 에 occurred_at 기간을 바인딩한다(period 여도 기간 미지정이면 스코프 없음). 'alltime' 이면
    기간 조건을 넣지 않는다. 알 수 없는 버킷은 방어적으로 ``FALSE`` 를 돌려 0행을 만든다(화이트리스트
    검증은 API 계층이 한다). 상태 문자열은 코드 상수라 사용자 입력이 아니다.

    Args:
        bucket: 운영 5버킷 중 하나. **모르는 값이면 0행 조건**을 돌려준다(전체를 반환하지 않는다).
        pfx: asset 테이블 접두사(조인 경로면 ``"a."``).
        relation_scope: ``period`` 면 관계 제안 시점을 자산 생성 기간으로 좁히고, ``alltime``
            이면 기간을 보지 않는다. period 라도 기간이 없으면 좁히지 않는다.
        since: 기간 시작(포함).
        until: 기간 끝(미포함).

    Returns:
        ``(WHERE 조각, 파라미터 목록)``. 둘의 순서가 맞물려야 하므로 함께 돌려준다.
    """
    if bucket == "processing":
        lits = ", ".join(f"'{s}'" for s in _PROCESSING_STATUSES)
        return f"{pfx}status IN ({lits})", []
    if bucket == "deferred":
        return f"{pfx}status = 'deferred'", []
    if bucket == "failed":
        return f"{pfx}status = 'failed'", []
    if bucket in ("registered", "relation_proposed"):
        # 두 버킷 모두 등록완료(registered) 자산 중 관계 제안 유무로 갈린다(상호배타·합=전체 registered).
        scoped = relation_scope == "period" and since is not None and until is not None
        exists = _relation_proposed_exists(pfx, scoped=scoped)
        neg = "" if bucket == "relation_proposed" else "NOT "
        params: list[Any] = [_RELATION_PROPOSED_ACTIVITY]
        if scoped:
            params += [since, until]
        return f"{pfx}status = 'registered' AND {neg}{exists}", params
    return "FALSE", []  # 모르는 버킷은 0행 — 조용히 전체를 반환하는 것보다 안전하다


def _period_clause(since: Any, until: Any) -> tuple[str, list[Any]]:
    """생성일(created_at) 기간 필터 WHERE 절·파라미터(단일 테이블 asset 전용·비한정).

    끝은 **미포함**이다 — 다른 조회들과 같은 규칙이라야 화면 곳곳의 수치가 맞물린다.

    Args:
        since: 시작(포함). ``None`` 이면 하한 없음.
        until: 끝(미포함). ``None`` 이면 상한 없음.

    Returns:
        ``(WHERE 절, 파라미터)``. 조건이 없으면 절은 빈 문자열이다.
    """
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("created_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("created_at < %s")
        params.append(until)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def asset_stats(conn: Any, *, since: Any = None, until: Any = None,
                snapshot_buckets: bool = False) -> dict[str, Any]:
    """전체 자산을 여러 기준으로 집계한다 — 상태·모달리티·도메인·확장자·날짜별과 총계.

    기간을 주면 **여섯 집계 전부**가 같은 기간으로 좁혀진다 — 하나만 전체 기간이면
    화면 안에서 수치가 서로 어긋난다. 끝은 미포함이다.

    5버킷 집계는 총계와 **같은 기간 조건**을 쓴다 — 그래야 버킷 합이 총계와 정확히 맞아,
    화면이 비율을 계산해도 100%가 된다. 단 '관계 제안됨' 판별만은 기간을 보지 않는다:
    기간 안에 만들어진 자산이면, 관계가 언제 제안됐든 제안된 자산으로 센다.

    Args:
        since: 생성일 시작(**포함**). ``None`` 이면 기간 제한 없음.
        until: 생성일 끝(**미포함**) — 하루 단위로 끊을 때 경계가 겹치지 않게 한다.
        snapshot_buckets: 5버킷 집계를 함께 낼지. **켜면 쿼리가 하나 더 돈다** —
            필요할 때만 켠다(끄면 응답 모양도 그대로다).

    Returns:
        ``{total, by_status, by_modality, by_domain, by_file_ext, by_date}``.
        ``snapshot_buckets=True`` 면 ``by_snapshot_bucket`` 이 추가되며, **0건 버킷도 항상**
        포함되고 그 합은 ``total`` 과 일치한다(화면이 비율을 계산할 수 있게).
    """
    where, p = _period_clause(since, until)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM asset {where}", p)
        total = int(cur.fetchone()[0])
        cur.execute(f"SELECT status, COUNT(*) FROM asset {where} "
                    "GROUP BY status ORDER BY COUNT(*) DESC, status ASC", p)
        by_status = [{"status": s, "count": int(c)} for s, c in cur.fetchall()]
        cur.execute(f"SELECT modality, COUNT(*) FROM asset {where} "
                    "GROUP BY modality ORDER BY COUNT(*) DESC, modality ASC", p)
        by_modality = [{"modality": m, "count": int(c)} for m, c in cur.fetchall()]
        cur.execute(f"SELECT domain_label, COUNT(*) FROM asset {where} "
                    "GROUP BY domain_label ORDER BY COUNT(*) DESC, domain_label ASC", p)
        by_domain = [{"domain": d, "count": int(c)} for d, c in cur.fetchall()]
        cur.execute(f"SELECT {_EXT_EXPR} AS ext, COUNT(*) FROM asset {where} "
                    "GROUP BY ext ORDER BY COUNT(*) DESC, ext ASC NULLS LAST", p)
        by_file_ext = [{"file_ext": e, "count": int(c)} for e, c in cur.fetchall()]
        cur.execute(f"SELECT created_at::date AS d, COUNT(*) FROM asset {where} "
                    "GROUP BY d ORDER BY d ASC", p)
        by_date = [{"date": d.isoformat() if d is not None else None, "count": int(c)}
                   for d, c in cur.fetchall()]
        result = {"total": total, "by_status": by_status, "by_modality": by_modality,
                  "by_domain": by_domain, "by_file_ext": by_file_ext, "by_date": by_date}
        if snapshot_buckets:
            result["by_snapshot_bucket"] = _snapshot_bucket_counts(cur, where, p)
    return result


def _snapshot_bucket_counts(cur: Any, where: str, period_params: list[Any]) -> list[dict[str, Any]]:
    """운영 5버킷 count 를 단일 FILTER 쿼리로 뽑아 ``_SNAPSHOT_BUCKETS`` 순서로 반환(0 포함).

    ``where``/``period_params`` = total 과 동일한 ``_period_clause``(created_at 기간·도메인 제외 없음) →
    버킷 자산집합이 total 과 같은 스코프라 ``sum==total`` 이 보장된다. 서브쿼리에서 자산별로
    관계 제안 여부를 먼저 계산한 뒤(기간 조건 없이) 바깥에서
    FILTER 로 5버킷을 한 번에 센다. 상태 문자열은 코드 상수라 사용자 입력이 아니다.

    Args:
        cur: 열려 있는 커서(호출부의 트랜잭션을 공유한다).
        where: 총계와 **같은 기간 절** — 달라지면 버킷 합이 총계와 어긋난다.
        period_params: 그 절에 대응하는 파라미터.

    Returns:
        5버킷 count 목록(0건 버킷도 포함·순서 고정).

    **파라미터 순서**: SELECT 리스트의 EXISTS(``activity=%s``)가 FROM/WHERE 의 기간 ``%s`` 보다 먼저
    등장하므로 ``[_RELATION_PROPOSED_ACTIVITY, *period_params]`` 순으로 바인딩한다(순서 어긋남 방지).
    FILTER 리스트 순서 = ``_SNAPSHOT_BUCKETS`` 순서(processing→deferred→registered→failed→relation_proposed).
    """
    proc = ", ".join(f"'{s}'" for s in _PROCESSING_STATUSES)  # 진행 중 상태 4종(classified 제외)
    sql = (
        f"SELECT "
        f"count(*) FILTER (WHERE status IN ({proc})), "                 # processing
        f"count(*) FILTER (WHERE status = 'deferred'), "               # deferred
        f"count(*) FILTER (WHERE status = 'registered' AND NOT rp), "  # registered
        f"count(*) FILTER (WHERE status = 'failed'), "                 # failed
        f"count(*) FILTER (WHERE status = 'registered' AND rp) "       # relation_proposed
        f"FROM (SELECT status, "
        f"EXISTS (SELECT 1 FROM asset_lineage l "
        f"WHERE l.asset_id = a.asset_id AND l.activity = %s) AS rp "
        f"FROM asset a {where}) t")
    cur.execute(sql, [_RELATION_PROPOSED_ACTIVITY, *period_params])
    counts = cur.fetchone()
    # strict=True: 버킷 수(5)와 FILTER 컬럼 수가 어긋나면 조용히 잘리지 않고 즉시 예외(회귀 방지).
    return [{"bucket": b, "count": int(c)} for b, c in zip(_SNAPSHOT_BUCKETS, counts, strict=True)]


def query_assets(conn: Any, *, status: str | None = None, modality: str | None = None,
                 domain: str | None = None, file_ext: str | None = None,
                 created_from: Any = None, created_to: Any = None,
                 snapshot_bucket: str | None = None, relation_scope: str = "period",
                 limit: int = 50, offset: int = 0, with_content: bool = False) -> dict[str, Any]:
    """자산 목록을 필터·페이징해 돌려준다(최신순).

    각 행에 확장자를 포함한다. **집계와 같은 식으로 뽑는다** — 다르면 목록과 통계가 어긋난다.
    파일명을 화면에서 다시 뜯지 않도록 표시용 필드도 함께 내린다.

    ⚠️ 두 테이블 모두 ``asset_id``·``created_at`` 컬럼을 가진다 — 조인이 붙는 경로에서 접두사를
    빼면 "어느 쪽 컬럼이냐"로 쿼리가 실패한다. 그래서 조인 경로는 ``a.`` 한정, 단일 테이블 경로는
    비한정으로 조건을 만든다. 조건과 값은 한 곳에 (템플릿, 값) 쌍으로 쌓아, 늘어놓는 순서가
    어긋날 수 없게 했다.

    Args:
        status: 처리 상태 필터. ``snapshot_bucket`` 을 함께 주면 **무시된다**.
        modality: 모달리티 필터.
        domain: 도메인 필터.
        file_ext: 확장자 필터(집계와 같은 식으로 뽑은 값과 비교한다).
        created_from: 생성일 시작(포함).
        created_to: 생성일 끝(미포함).
        snapshot_bucket: 운영 5버킷으로 묶어 필터. **``status`` 보다 우선**하며, 모르는
            값이면 0행을 돌려준다(전체를 반환하지 않는다).
        relation_scope: ``period``(기본)면 관계 제안 여부를 자산 생성 기간 안에서만 보고,
            ``alltime`` 이면 기간과 무관하게 본다.
        limit: 페이지 크기.
        offset: 건너뛸 행 수.
        with_content: 켜면 행마다 요약·키워드가 붙는다(메타 테이블을 조인). 메타가 없는
            자산도 행은 남고 그 값만 ``None`` 이다. **끄면 조인 없이 가볍게** 돈다.

    Returns:
        ``{rows, total, limit, offset}``. ``total`` 은 같은 필터로 센 전체 건수라 화면의
        쪽수와 목록이 어긋나지 않는다. ``rows`` 는 생성 최신순.
    """
    # (조건템플릿, 파라미터리스트)를 한 곳에서 누적 → 조건 순서와 파라미터 순서가 구조적으로 일치(불변식 내재화).
    # 템플릿의 ``{a}`` = 테이블 접두사 자리 — 전 경로 "a." 별칭 통일(EXISTS 상관 버그 방지·아래 _where).
    # 값리스트는 execute 시 순서대로 확장(extend). specs 가 비면 _where 가 WHERE 절을 만들지 않는다.
    specs: list[tuple[str, list[Any]]] = []
    if snapshot_bucket:
        # 버킷이 주어지면 상태 조건 대신 버킷 조건을 쓴다(둘을 함께 걸면 서로 모순될 수 있다).
        # 버킷 술어 파라미터 순서(activity→occurred_since→occurred_until)를 specs 값리스트에 그대로 실어
        # WHERE 순서와 파라미터 순서가 어긋나지 않게 한다({a} 접두로 content/비content 경로 공용).
        frag, bp = _snapshot_bucket_predicate(
            snapshot_bucket, "{a}", relation_scope=relation_scope,
            since=created_from, until=created_to)
        specs.append((frag, bp))
    elif status:
        specs.append(("{a}status = %s", [status]))
    if modality:
        specs.append(("{a}modality = %s", [modality]))
    if domain:
        specs.append(("{a}domain_label = %s", [domain]))
    if file_ext:
        # {a} 자리표시자 유지 — 아래 _where 가 .format(a=pfx) 로 접두("" / "a.")를 채운다.
        specs.append((ext_expr("{a}") + " = %s", [file_ext]))
    if created_from is not None:
        specs.append(("{a}created_at >= %s", [created_from]))
    if created_to is not None:
        specs.append(("{a}created_at < %s", [created_to]))
    params = [v for _t, vs in specs for v in vs]

    def _where(pfx: str) -> str:
        """조건들을 테이블 접두사와 함께 WHERE 절로 만든다.

        Args:
            pfx: 컬럼 접두사. 조인 경로에서는 ``"a."`` 를 줘야 컬럼이 모호해지지 않는다.

        Returns:
            WHERE 절. 조건이 없으면 빈 문자열.
        """
        if not specs:
            return ""
        return " WHERE " + " AND ".join(t.format(a=pfx) for t, _vs in specs)

    with conn.cursor() as cur:
        # COUNT·경량목록도 ``asset a`` 별칭 + _where("a.") — EXISTS 상관(l.asset_id=a.asset_id)이
        # 비한정이면 asset_lineage 동명 컬럼에 바인딩돼 상관이 풀리던 버그를 별칭 통일로 봉인.
        cur.execute("SELECT COUNT(*) FROM asset a" + _where("a."), params)
        total = int(cur.fetchone()[0])
        # 행에도 확장자를 실어 준다 — 집계와 **같은 식**을 써야 목록·통계가 맞물린다
        # 로 파생해 행 file_ext == 집계 버킷 키(프론트 파일명 파싱·폴백 확장자 집계 제거·admin B2).
        # content JOIN 경로에서도 fs_path 는 asset 에만 있어 비한정 참조가 모호하지 않다(단일 출처식).
        if with_content:
            cur.execute(
                "SELECT a.asset_id, a.status, a.modality, a.domain_label, a.fs_path, a.created_at, "
                "m.ext_meta->>'summary' AS summary, m.ext_meta->'keywords' AS keywords, "
                + _EXT_EXPR + " AS file_ext "
                "FROM asset a LEFT JOIN asset_metadata m ON m.asset_id = a.asset_id"
                + _where("a.") + " ORDER BY a.created_at DESC, a.asset_id DESC LIMIT %s OFFSET %s",
                [*params, limit, offset])
            rows = [
                {"asset_id": str(aid), "status": st, "modality": mod, "domain_label": dl,
                 "file_name": display_file_name(fp) if fp else None,
                 "created_at": ts.isoformat() if ts is not None else None,
                 "summary": summary, "keywords": kw, "file_ext": fx}
                for aid, st, mod, dl, fp, ts, summary, kw, fx in cur.fetchall()]
        else:
            cur.execute(
                "SELECT asset_id, status, modality, domain_label, fs_path, created_at, "
                + _EXT_EXPR + " AS file_ext FROM asset a"
                + _where("a.") + " ORDER BY created_at DESC, asset_id DESC LIMIT %s OFFSET %s",
                [*params, limit, offset])
            rows = [
                {"asset_id": str(aid), "status": st, "modality": mod, "domain_label": dl,
                 "file_name": display_file_name(fp) if fp else None,
                 "created_at": ts.isoformat() if ts is not None else None, "file_ext": fx}
                for aid, st, mod, dl, fp, ts, fx in cur.fetchall()]
    # 페이징 응답 모양을 통일한다({rows,total,limit,offset}) — 화면이 맨앞·맨끝으로 이동하려면 total 이 필요하다.
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def modality_detail(conn: Any, modality: str, *, since: Any = None,
                    until: Any = None) -> dict[str, Any]:
    """한 모달리티 안을 파고들어 집계한다 — 총계와 확장자·상태·일자별 분포.

    예: 영상 안에서 mp4/mov 비율, 처리 상태 분포, 날짜별 추이.

    Args:
        modality: 대상 모달리티.
        since: 생성일 시작(포함). 개요 집계와 **같은 기준**이라 두 화면 수치가 맞물린다.
        until: 생성일 끝(미포함).

    Returns:
        ``{total, by_file_ext, by_status, by_date}``.
    """
    conds = ["modality = %s"]
    p: list[Any] = [modality]
    if since is not None:
        conds.append("created_at >= %s")
        p.append(since)
    if until is not None:
        conds.append("created_at < %s")
        p.append(until)
    # 아래 네 질의가 **같은 조건·같은 값**을 공유한다 — 하나라도 다르면 총계와 분포가 어긋난다.
    where = "WHERE " + " AND ".join(conds)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM asset {where}", p)
        total = int(cur.fetchone()[0])
        cur.execute(f"SELECT {_EXT_EXPR} AS ext, COUNT(*) FROM asset {where} "
                    "GROUP BY ext ORDER BY COUNT(*) DESC, ext ASC NULLS LAST", p)
        by_file_ext = [{"file_ext": e, "count": int(c)} for e, c in cur.fetchall()]
        cur.execute(f"SELECT status, COUNT(*) FROM asset {where} "
                    "GROUP BY status ORDER BY COUNT(*) DESC, status ASC", p)
        by_status = [{"status": s, "count": int(c)} for s, c in cur.fetchall()]
        cur.execute(f"SELECT created_at::date AS d, COUNT(*) FROM asset {where} "
                    "GROUP BY d ORDER BY d ASC", p)
        by_date = [{"date": d.isoformat() if d is not None else None, "count": int(c)}
                   for d, c in cur.fetchall()]
    return {"modality": modality, "total": total, "by_file_ext": by_file_ext,
            "by_status": by_status, "by_date": by_date}


def asset_timeline(conn: Any, *, since: Any = None, until: Any = None,
                   interval: str = "day", group_by: str | None = None,
                   modality: str | None = None) -> dict[str, Any]:
    """자산이 언제 얼마나 만들어졌는지 추이를 낸다.

    Args:
        since: 기간 시작(포함).
        until: 기간 끝(미포함).
        interval: 버킷 단위. **허용 목록 밖이면 일 단위로 접는다** — SQL 에 문자열로 박히는 값이라
            임의 입력을 그대로 쓰지 않는다.
        group_by: 시리즈를 가를 기준(모달리티·상태·도메인). 주면 응답이 **여러 시리즈**로 바뀐다.
        modality: 특정 모달리티로 범위를 좁힌다.

    Returns:
        단일: ``{interval, buckets}`` / 다중: 시리즈 목록. 시리즈·버킷 모두 정렬이 고정된다.
    """
    # ⚠️ 버킷 단위는 아래 SQL 에 **문자열로 직접 박힌다** — 허용 목록을 통과한 값만 쓴다.
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
    # 조건과 값을 짝지어 쌓는다 — 따로 관리하면 늘어놓는 순서가 어긋나 엉뚱한 값이 박힌다.
    conds: list[str] = []
    params: list[Any] = []
    if modality:
        conds.append("modality = %s")
        params.append(modality)
    if since is not None:
        conds.append("created_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("created_at < %s")
        params.append(until)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with conn.cursor() as cur:
        # 시리즈를 가르면 응답 모양 자체가 달라진다(단일 buckets → series 배열).
        if group_by in _GROUP_COLS:
            # 컬럼명도 SQL 에 직접 박히므로 매핑을 통과한 값만 쓴다.
            gcol = _GROUP_COLS[group_by]
            cur.execute(
                f"SELECT {gcol} AS key, date_trunc('{trunc}', created_at) AS bkt, COUNT(*) "
                f"FROM asset{where} GROUP BY key, bkt ORDER BY key ASC, bkt ASC", params)
            return {"interval": trunc, "group_by": group_by, "series": pivot_series(cur.fetchall())}
        cur.execute(f"SELECT date_trunc('{trunc}', created_at) AS bkt, COUNT(*) "
                    f"FROM asset{where} GROUP BY bkt ORDER BY bkt ASC", params)
        buckets = [{"bucket": b.isoformat() if b is not None else None, "count": int(c)}
                   for b, c in cur.fetchall()]
        return {"interval": trunc, "buckets": buckets}


def build_modality_overview(conn: Any, modality: str, *, since: Any = None, until: Any = None,
                            interval: str = "day", limit: int = 50) -> dict[str, Any]:
    """모달리티 현황 화면이 필요한 셋(집계·추이·첫 페이지)을 **한 트랜잭션에서 한 번에** 만든다.

    화면이 서너 번 부르던 것을 한 번으로 묶는다. 계산은 검증된 조회 함수 셋을 그대로 재사용하며,
    **세 조각 모두 같은 모달리티·같은 기간**으로 맞춰 화면 안에서 수치가 어긋나지 않게 한다.

    - ``detail``: ``modality_detail`` — 확장자·상태·일자 분포 + 총계.
    - ``timeline``: 생성 추이 단일 시리즈(``{interval, buckets}``).
    - ``first_page``: 목록 첫 페이지(요약·키워드 동반).

    Args:
        conn: 열려 있는 연결 — 세 조각이 같은 트랜잭션을 공유해야 수치가 어긋나지 않는다.
        modality: 대상 모달리티.
        since: 기간 시작(포함). ``None`` 이면 전체 기간.
        until: 기간 끝(미포함).
        interval: 추이를 끊을 단위. **허용 밖 값은 조용히 일 단위로 접힌다** — 값 검증은
            API 계층(422)이 맡는다.
        limit: 첫 페이지에 담을 행 수.

    Returns:
        ``{detail, timeline, first_page}``.
    """
    detail = modality_detail(conn, modality, since=since, until=until)
    timeline = asset_timeline(conn, since=since, until=until, interval=interval, modality=modality)
    first_page = query_assets(
        conn, modality=modality, created_from=since, created_to=until,
        with_content=True, limit=limit, offset=0)
    return {"detail": detail, "timeline": timeline, "first_page": first_page}
