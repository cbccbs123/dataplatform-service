"""013 US4 — 자산/FSM 대시보드 집계·목록 조회. 읽기 전용·결정적(헌법 3·6조)·LLM 0.

자산 데이터·스키마는 쓰기 0(SELECT only). 도메인 제외 없음(2026-07-23 전면 제거·의료 특수 트랙 미운용).
정렬은 COUNT(*) DESC + key ASC / created_at DESC + asset_id DESC tiebreak 으로 결정적이다.
"""
from __future__ import annotations

from typing import Any

from src.config.filename_util import (
    display_file_name,  # 표시 파일명 asset_id 프리픽스 제거(065 T605)
)
from service.portal._ext_expr import ext_expr  # 확장자 SQL 정규식 단일 출처(069 D4·057 FR-104)
from service.portal._timeline_util import TIMELINE_INTERVALS, pivot_series

# 도메인 제외 없음(2026-07-23 전면 제거) — 의료 특수 트랙 미운용이라 통계·목록은 도메인 무관 균일 집계.
# 파일 확장자(file_ext) = fs_path 마지막 .세그먼트(소문자·없으면 NULL). 단일 출처 ext_expr(비한정 fs_path).
_EXT_EXPR = ext_expr()

# 054 관리자 스냅샷 버킷(계보 현황 화면) — FSM status 를 운영 관점 5버킷으로 롤업.
# 버킷 순서 = 응답/집계 열거 순서(결정적). relation_proposed 는 registered 중 관계 제안이 있는 하위집합.
_SNAPSHOT_BUCKETS = ("processing", "deferred", "registered", "failed", "relation_proposed")
# processing = "진행 중" FSM 상태 4종. C1: classified 는 status 값이 아니라 분류 결과 표식이므로 제외.
_PROCESSING_STATUSES = ("received", "routing", "classifying", "extracting")
# relation_proposed 판별용 계보 activity(자산에 관계 제안이 붙은 lineage 기록).
_RELATION_PROPOSED_ACTIVITY = "relations.proposed.v1"
_RELATION_SCOPES = ("period", "alltime")  # 검증은 API 계층(G3) 책임·여기선 값만 사용
# 자산 생성 추이 group_by 화이트리스트 → 컬럼식(고정 매핑·사용자 입력은 키로만 조회·인젝션 안전).
# file_ext 는 평컬럼이 아닌 확장자 정규식(_EXT_EXPR) — 단일 테이블(asset) 쿼리라 fs_path 비한정 안전.
_GROUP_COLS = {"modality": "modality", "status": "status", "domain": "domain_label",
               "file_ext": _EXT_EXPR}


def _relation_proposed_exists(pfx: str, *, scoped: bool) -> str:
    """asset_lineage 에 relation 제안 기록이 있는지 검사하는 EXISTS 조각.

    ``pfx`` = asset 테이블 접두("" 단일 테이블 / "a." JOIN 모호성 방지). EXISTS 서브쿼리의
    ``asset_lineage l`` 은 자체 alias 라 바깥 asset 컬럼과 모호성이 없다. ``scoped`` 면 관계 제안이
    발생한 기간(occurred_at)을 ``%s,%s`` 로 바인딩(요청 §2.2: 자산 created 기간 = 관계 occurred 기간).
    ``%s`` 순서 = activity → (scoped 면) occurred_since → occurred_until.
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
    검증은 API 계층 G3 책임). status 리터럴은 고정 SQL(사용자 입력 아님)·인젝션 안전.
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
    return "FALSE", []  # 방어적: 미지의 버킷은 0행(정상 경로는 G3 화이트리스트로 차단)


def _period_clause(since: Any, until: Any) -> tuple[str, list[Any]]:
    """생성일(created_at) 기간 필터 WHERE 절·파라미터(단일 테이블 asset 전용·비한정).

    to(until) 는 exclusive(``< %s``) — query_assets·timeline·다른 API 와 동일 규칙. 미지정이면 전체.
    (2026-07-23: 도메인 제외 전면 제거 — 조건이 하나도 없으면 WHERE 절을 아예 만들지 않는다.)
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
    """전체 자산 집계(status·modality·domain·file_ext·date별·총계·결정적·FR-009e·도메인 제외 없음).

    ``since``/``until``(생성일 from/to·to exclusive·보완 v6) 지정 시 6개 집계 전부 기간 스코프
    (대시보드 기간 필터가 파일 포맷·모달리티·일자 분포에 일관 반영). 미지정이면 전체 기간.

    ``snapshot_buckets=True``(054·계보 현황 화면·FR-201/202) 지정 시 응답에 ``by_snapshot_bucket``
    (운영 5버킷 count·``_SNAPSHOT_BUCKETS`` 순서·0건도 항상 포함)을 추가한다. 버킷 자산집합은 total 과
    **동일한 ``_period_clause`` 스코프**(created_at 기간·도메인 제외 없음)라 ``sum(by_snapshot_bucket)==total``
    이 보장된다. relation_proposed 판별(EXISTS)만은 관계 제안 유무를 전 기간에서 보는 alltime(FR-202:
    자산 created 기간 안이면 과거 관계 제안도 반영). ``snapshot_buckets=False``(기본)면 기존 응답·SQL 이
    완전히 불변이다(하위호환·FILTER 쿼리 미실행).
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

    ``where``/``period_params`` = total 과 동일한 ``_period_clause``(의료 제외 + created_at 기간) →
    버킷 자산집합이 total 과 같은 스코프라 ``sum==total`` 이 보장된다. 서브쿼리에서 자산별로
    관계 제안 존재 여부(rp)를 EXISTS 로 계산(FR-202: alltime·occurred_at 기간 없음)한 뒤 바깥에서
    FILTER 로 5버킷을 한 번에 센다. status IN 리터럴은 ``_PROCESSING_STATUSES`` 고정 SQL(인젝션 안전).

    **파라미터 순서**: SELECT 리스트의 EXISTS(``activity=%s``)가 FROM/WHERE 의 기간 ``%s`` 보다 먼저
    등장하므로 ``[_RELATION_PROPOSED_ACTIVITY, *period_params]`` 순으로 바인딩한다(순서 어긋남 방지).
    FILTER 리스트 순서 = ``_SNAPSHOT_BUCKETS`` 순서(processing→deferred→registered→failed→relation_proposed).
    """
    proc = ", ".join(f"'{s}'" for s in _PROCESSING_STATUSES)  # C1: classified 제외 4status 리터럴
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
    """자산 목록(FSM 단계·modality·domain·file_ext·날짜 필터·페이징·의료 제외·created_at DESC·FR-009f).

    각 행은 ``file_ext``(fs_path 확장자·057 FR-104·by_file_ext 집계와 동일 파생)를 포함한다 —
    프론트가 파일명을 다시 파싱하지 않도록 표시필드를 하향(하위호환 필드 추가).

    ``with_content=True``(보완 v6) — asset_metadata LEFT JOIN 으로 행마다 요약·키워드(+제목=파일명)
    동반(모달리티 상세에서 자산을 안 열고도 내용 파악). 메타 미적재 자산은 LEFT JOIN 으로 행은 남되
    summary/keywords 가 None. 기본은 가벼운 목록(하위호환).

    ``snapshot_bucket``(054·계보 현황) 지정 시 status 를 운영 5버킷으로 롤업해 필터한다. 이때
    ``status`` 인자는 **무시**(C3: 버킷 우선)하고 버킷 술어를 대신 넣는다. ``relation_scope='period'``
    (기본)이면 relation_proposed/registered 판별의 관계 제안 기간을 자산 created 기간(created_from/to)에
    맞춰 스코프하고, ``'alltime'`` 이면 전 기간에서 관계 제안 유무만 본다. bucket 화이트리스트/relation_scope
    검증은 API 계층(G3) 책임이며, 여기서는 알 수 없는 버킷을 방어적으로 0행 처리한다.
    ``snapshot_bucket=None`` 이면 기존 동작·SQL 이 완전히 불변이다(하위호환).

    **모호성 주의**: ``asset`` 과 ``asset_metadata`` 둘 다 ``asset_id``·``created_at`` 컬럼을 가져,
    content 경로의 JOIN 에서 비한정 컬럼은 PG 오류가 난다. 그래서 content WHERE/SELECT/ORDER BY 는
    ``a.`` 한정(``_where("a.")``), COUNT·비콘텐츠 SELECT 는 단일 테이블이라 비한정(``_where("")``)으로 쓴다.
    조건·파라미터는 ``specs`` 한 곳에서 (템플릿, 값리스트) 쌍으로 누적해 순서 불변식을 구조적으로 보장한다.
    """
    # (조건템플릿, 파라미터리스트)를 한 곳에서 누적 → 조건 순서와 파라미터 순서가 구조적으로 일치(불변식 내재화).
    # 템플릿의 ``{a}`` = 테이블 접두사 자리 — 전 경로 "a." 별칭 통일(EXISTS 상관 버그 방지·아래 _where).
    # 값리스트는 execute 시 순서대로 확장(extend). specs 가 비면 _where 가 WHERE 절을 만들지 않는다.
    specs: list[tuple[str, list[Any]]] = []
    if snapshot_bucket:
        # C3: snapshot_bucket 우선 — status 스펙은 추가하지 않고 버킷 술어로 대체한다.
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

    def _where(pfx: str) -> str:  # specs 를 접두사 채워 WHERE 로. 조건 0 개면 절 자체를 만들지 않는다.
        if not specs:
            return ""
        return " WHERE " + " AND ".join(t.format(a=pfx) for t, _vs in specs)

    with conn.cursor() as cur:
        # COUNT·경량목록도 ``asset a`` 별칭 + _where("a.") — EXISTS 상관(l.asset_id=a.asset_id)이
        # 비한정이면 asset_lineage 동명 컬럼에 바인딩돼 상관이 풀리던 버그를 별칭 통일로 봉인.
        cur.execute("SELECT COUNT(*) FROM asset a" + _where("a."), params)
        total = int(cur.fetchone()[0])
        # FR-104(057): 행에 file_ext 하향(하위호환) — by_file_ext 집계와 동일한 _EXT_EXPR(fs_path 확장자)
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
    # FR-701: 페이징 봉투 통일({rows,total,limit,offset}) — 프론트 전체 목록 페이지/맨앞·맨끝 이동 계약.
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def modality_detail(conn: Any, modality: str, *, since: Any = None,
                    until: Any = None) -> dict[str, Any]:
    """단일 모달리티 스코프 집계(보완 v6) — 총계 + 확장자·상태·일자별. 결정적·LLM 0(도메인 제외 없음).

    모달리티 드릴다운(예: video 안에서 mp4/mov 분포·일자 추이·FSM 상태). modality 는 %s 바인딩.
    ``since``/``until``(생성일 from/to·to exclusive) 지정 시 개요(asset_stats) 기간 필터와 일관 스코프.
    """
    conds = ["modality = %s"]
    p: list[Any] = [modality]
    if since is not None:
        conds.append("created_at >= %s")
        p.append(since)
    if until is not None:
        conds.append("created_at < %s")
        p.append(until)
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
    """자산 생성 일자 추이(보완 v6·계보 timeline 과 동일 멀티시리즈 패턴). 결정적·LLM 0(도메인 제외 없음).

    ``group_by``(modality/status/domain) 주면 멀티시리즈(시리즈 key ASC·버킷 ASC), 미지정이면 단일
    시리즈({interval, buckets}). trunc 화이트리스트(f-string 안전)·기간(since/until)은 %s 바인딩.

    ``modality``(057 FR-302·모달리티 현황 BFF timeline) 지정 시 그 모달리티로 스코프한다(WHERE modality=%s·
    %s 바인딩). 미지정(기본 None)이면 기존 SQL·동작이 완전히 불변이다(하위호환·바이트 동일).
    """
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
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
        if group_by in _GROUP_COLS:
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
    """모달리티 현황 BFF(057 FR-302) — 드릴다운 집계 + 생성 추이 + 첫 페이지 목록을 **한 트랜잭션·1회 응답**.

    프론트 모달리티 상세가 stats+timeline+first-page 를 3~4회 순차 호출하던 것을 묶는다. 검증된 순수
    조회 함수 3종을 그대로 재사용해 재구현 0·의료 제외 상속·결정성·LLM 0(``build_dashboard_summary``
    조합 패턴). 세 슬라이스 전부 같은 modality/기간(created_at)으로 스코프해 화면 정합을 보장한다.

    - ``detail``: ``modality_detail`` — 확장자·상태·일자 분포 + 총계.
    - ``timeline``: ``asset_timeline(modality=…)`` — 생성 추이(interval=month 지원 → 프론트 일→월 롤업
      제거·FR-303 동형). 단일 시리즈({interval, buckets}).
    - ``first_page``: ``query_assets(with_content=True)`` 첫 페이지(요약·키워드 동반·페이징 봉투).

    ``interval`` 은 ``asset_timeline`` 화이트리스트(day/hour/month·그 외 day 폴백; API 계층 422).
    """
    detail = modality_detail(conn, modality, since=since, until=until)
    timeline = asset_timeline(conn, since=since, until=until, interval=interval, modality=modality)
    first_page = query_assets(
        conn, modality=modality, created_from=since, created_to=until,
        with_content=True, limit=limit, offset=0)
    return {"detail": detail, "timeline": timeline, "first_page": first_page}
