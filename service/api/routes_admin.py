"""관리자 조회 라우트 (069 US-E FR-E6·A) — 계보·접근이력·자산집계·대시보드·관계 목록(GET).

종전 ``portal_api.py`` 의 ``/admin/*`` GET 핸들러를 그대로 이관한다(동작 불변). 서비스 함수는 각 홈에서
직접 import 하고, 인프라(``_run_in_db``·``_parse_dt``·``_validated_interval``)는 ``_infra`` 모듈참조로
쓴다 — 그래야 단위 테스트가 ``patch("service.api.routes_admin.<name>")`` 로 쓰는 곳에서 대체할 수 있다.

⚠️ 라우트 등록 순서(catch-all): ``/admin/assets/modality/{modality}`` 계열·``/admin/assets/{asset_id}/lineage``
를 먼저 선언하고 catch-all 1세그 ``/admin/assets/{asset_id}`` 를 **뒤**에 둔다(구체 경로 우선 매칭 — C8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import _infra
from service.portal.access_log import (
    access_log_overview,
    access_log_stats,
    access_log_timeline,
    query_access_logs,
)
from service.portal.asset_detail import fetch_asset_detail
from service.portal.asset_stats import (
    _RELATION_SCOPES,
    _SNAPSHOT_BUCKETS,
    asset_stats,
    asset_timeline,
    build_modality_overview,
    modality_detail,
    query_assets,
)
from service.portal.auth import Principal, require_principal
from service.portal.dashboard import build_dashboard_summary
from service.portal.lineage_query import (
    lineage_timeline,
    query_asset_lineage,
    query_lineage_feed,
    relation_proposed_summary,
)
from src.relations.review import _REVIEW_STATUSES, list_edges_for_review, list_relation_kinds

router = APIRouter()

# relation_kind status 화이트리스트(필터 드롭다운 GET·FR-801). 관계 어휘 두 상태만 노출한다.
_RELATION_KIND_STATUSES = ("active", "inactive")


@router.get("/admin/assets/{asset_id}/lineage")
def asset_lineage(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 처리 이력(계보)을 발생 시각순으로 반환한다(013 US2·FR-004).

    조회 전용(``_run_in_db`` idempotent) — ``query_asset_lineage`` 가 ``asset_lineage`` 를
    시간순으로 끌어온다. 자산 데이터·스키마 쓰기 0·신규 LLM 0. 도메인 제외 없음(2026-07-23·FR-014 폐지).
    미존재/이력 없음은 빈 ``activities`` 로 200 반환(의도·도메인 제외 없음).
    """
    activities = _infra._run_in_db(lambda conn: query_asset_lineage(conn, asset_id))
    return {"asset_id": asset_id, "activities": activities}


@router.get("/admin/access-logs")
def access_logs(
    user: str | None = Query(None, description="사용자 id 필터"),
    action: str | None = Query(None, description="동작 필터(search/asset_view/download/bundle)"),
    from_: str | None = Query(None, alias="from", description="기간 하한(YYYY-MM-DD 또는 ISO)"),
    to: str | None = Query(None, alias="to", description="기간 상한"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """API 접근 이력 조회(필터·페이징·013 US3·FR-009). 조회 전용·결정적·LLM 0.

    접근 정책(013 D4): **인증된 사용자 누구나**(``require_principal``·현 2-tier MVP). 감사 데이터는
    clearance 별 마스킹 없이 전사 노출 — admin/operator 한정은 RBAC 도입 시(향후 포탈) 조인다(의도적 개방).
    """
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: query_access_logs(
            conn, user_id=user, action=action, since=since, until=until,
            limit=limit, offset=offset,
        )
    )


@router.get("/admin/access-logs/stats")
def access_logs_stats(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """접근 이력 기본 집계(총계·action별·user별·013 FR-009a). 조회 전용·결정적·LLM 0."""
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(lambda conn: access_log_stats(conn, since=since, until=until))


@router.get("/admin/access-logs/timeline")
def access_logs_timeline(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
    interval: Annotated[str, Depends(_infra._validated_interval)] = "day",
    action: str | None = Query(None, description="단일 api 필터(search/asset_view/download/bundle)"),
    group_by: str | None = Query(None, description="멀티시리즈 분할: action | user_id(미지정=단일)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """접근 이력 시계열(버킷별 호출 수·그래프용·013 FR-009c). 조회 전용·결정적·LLM 0.

    ``group_by=action``(또는 user_id)이면 멀티시리즈 1회 응답(시리즈별 막대). 미지정이면 단일 시리즈.
    interval 화이트리스트 검증은 ``_validated_interval`` Depends(422·FR-E6 통합).
    """
    if group_by is not None and group_by not in ("action", "user_id"):
        raise HTTPException(status_code=422, detail=f"group_by 는 action|user_id 만 허용: {group_by!r}")
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: access_log_timeline(
            conn, since=since, until=until, action=action, interval=interval, group_by=group_by))


@router.get("/admin/access-logs/overview")
def access_logs_overview_endpoint(
    from_: str | None = Query(None, alias="from", description="기간 하한(YYYY-MM-DD 또는 ISO)"),
    to: str | None = Query(None, alias="to", description="기간 상한(exclusive)"),
    action: str | None = Query(None, description="추이 드릴다운 action(총계/action별 KPI 는 기간 전체)"),
    interval: Annotated[str, Depends(_infra._validated_interval)] = "day",
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """접근 이력 overview 1회 응답(057 FR-301) — ``{total, by_action, timeline}``. 조회 전용·결정적·LLM 0.

    프론트가 stats+list+timeline 3회 순차 호출하던 것을 stats+timeline **1회**로 묶는다(``access_log_overview``·
    list 는 별도 페이징 유지). interval 화이트리스트 위반은 422(``_validated_interval``).
    """
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: access_log_overview(
            conn, since=since, until=until, action=action, interval=interval))


@router.get("/admin/lineage")
def lineage_feed(
    from_: str | None = Query(None, alias="from", description="기간 하한(YYYY-MM-DD 또는 ISO)"),
    to: str | None = Query(None, alias="to", description="기간 상한"),
    activity: str | None = Query(None, description="활동명 필터(예: ingest.registered.v1)"),
    modality: str | None = Query(None, description="자산 모달리티 필터(text/image/video/audio 등)"),
    status: str | None = Query(None, description="자산 FSM 단계 필터(registered/failed 등)"),
    file_ext: str | None = Query(None, description="자산 파일 확장자 필터(예: txt, pdf, mp4)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """기간 내 전 자산 계보 피드(시간역순·페이징·013 FR-009b). 조회 전용·결정적·LLM 0·도메인 제외 없음."""
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: query_lineage_feed(
            conn, since=since, until=until, activity=activity, modality=modality,
            status=status, file_ext=file_ext, limit=limit, offset=offset))


@router.get("/admin/lineage/timeline")
def lineage_timeline_endpoint(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
    activity: str | None = Query(None, description="활동명 필터"),
    interval: Annotated[str, Depends(_infra._validated_interval)] = "day",
    group_by: str | None = Query(None, description="멀티시리즈 분할: activity | modality | status(미지정=단일)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """계보 시계열(누적 막대 차트 1회·access timeline 과 대칭). 결정적·LLM 0·도메인 제외 없음.

    ``group_by``(activity/modality/status) 주면 멀티시리즈, 미지정이면 단일 시리즈. interval 검증은
    ``_validated_interval`` Depends(422).
    """
    if group_by is not None and group_by not in ("activity", "modality", "status"):
        raise HTTPException(status_code=422, detail=f"group_by 는 activity|modality|status 만: {group_by!r}")
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: lineage_timeline(
            conn, since=since, until=until, activity=activity, interval=interval, group_by=group_by))


@router.get("/admin/asset-stats")
def asset_stats_endpoint(
    from_: str | None = Query(None, alias="from", description="생성일 하한(YYYY-MM-DD 또는 ISO)"),
    to: str | None = Query(None, alias="to", description="생성일 상한(exclusive)"),
    snapshot_buckets: bool = Query(
        False, description="운영 5버킷 집계(by_snapshot_bucket) 동반(계보 현황 화면·054·FR-201/202)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """전체 자산 집계(FSM status·modality·domain·file_ext·date별·총계·013 FR-009e). 결정적·LLM 0·도메인 제외 없음.

    ``snapshot_buckets=true``(054·계보 현황) 지정 시 응답에 ``by_snapshot_bucket``(운영 5버킷
    count·``sum==total``·FR-201/202)이 추가된다. 미지정(기본 False)이면 기존 응답이 완전히 불변이다.
    """
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: asset_stats(
            conn, since=since, until=until, snapshot_buckets=snapshot_buckets))


@router.get("/admin/assets")
def assets_list(
    status: str | None = Query(None, description="FSM 단계 필터(received/registered/failed 등)"),
    modality: str | None = Query(None, description="모달리티 필터(text/image/video/audio 등)"),
    domain: str | None = Query(None, description="도메인 필터(general/review/medical 등)"),
    file_ext: str | None = Query(None, description="파일 확장자 필터(예: txt, pdf, mp4)"),
    created_from: str | None = Query(None, description="생성일 하한(YYYY-MM-DD 또는 ISO)"),
    created_to: str | None = Query(None, description="생성일 상한"),
    snapshot_bucket: str | None = Query(
        None, description="운영 스냅샷 버킷 필터(processing/deferred/registered/failed/"
                          "relation_proposed·054). 지정 시 status 대신 버킷으로 롤업 필터(C3)"),
    relation_scope: str = Query(
        "period", description="relation_proposed/registered 관계 제안 판별 스코프: "
                             "period(기본·자산 created 기간) | alltime(전 기간)"),
    with_content: bool = Query(False, description="행마다 요약·키워드 동반(모달리티 상세·보완 v6)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 목록(FSM·modality·domain·file_ext·날짜 필터·페이징·013 FR-009f). 도메인 제외 없음·created_at 역순·LLM 0.

    ``with_content=true`` 면 행마다 요약(summary)·키워드(keywords) 동반. ``snapshot_bucket``(054·FR-103)
    지정 시 FSM status 를 운영 5버킷으로 롤업해 필터(``status`` 무시·C3). 화이트리스트 검증(400)은 이
    API 계층 책임(f-string 인젝션 방지). 둘 다 미지정 시 기존 동작 불변(하위호환).
    """
    if snapshot_bucket is not None and snapshot_bucket not in _SNAPSHOT_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 snapshot_bucket: {snapshot_bucket!r} (허용: {list(_SNAPSHOT_BUCKETS)})")
    if relation_scope not in _RELATION_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 relation_scope: {relation_scope!r} (허용: {list(_RELATION_SCOPES)})")
    cfrom, cto = _infra._parse_dt(created_from), _infra._parse_dt(created_to)
    return _infra._run_in_db(
        lambda conn: query_assets(
            conn, status=status, modality=modality, domain=domain, file_ext=file_ext,
            created_from=cfrom, created_to=cto, snapshot_bucket=snapshot_bucket,
            relation_scope=relation_scope, limit=limit, offset=offset,
            with_content=with_content))


@router.get("/admin/assets/modality/{modality}")
def modality_detail_endpoint(
    modality: str,
    from_: str | None = Query(None, alias="from", description="생성일 하한"),
    to: str | None = Query(None, alias="to", description="생성일 상한(exclusive)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """모달리티 드릴다운 집계(보완 v6) — 해당 모달리티의 확장자·상태·일자별 분포 + 총계. 결정적·LLM 0·도메인 제외 없음."""
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(lambda conn: modality_detail(conn, modality, since=since, until=until))


@router.get("/admin/assets/modality/{modality}/overview")
def modality_overview_endpoint(
    modality: str,
    from_: str | None = Query(None, alias="from", description="생성일 하한(YYYY-MM-DD 또는 ISO)"),
    to: str | None = Query(None, alias="to", description="생성일 상한(exclusive)"),
    interval: Annotated[str, Depends(_infra._validated_interval)] = "day",
    limit: int = Query(50, ge=1, le=200),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """모달리티 현황 BFF 1회 응답(057 FR-302) — ``{detail, timeline, first_page}``. 조회 전용·도메인 제외 없음·LLM 0.

    interval 화이트리스트 위반은 422(``_validated_interval``). **라우트 순서(C8)**: 리터럴 3세그 경로라
    catch-all 1세그 ``/admin/assets/{asset_id}`` 보다 위(구체 경로)에 둔다.
    """
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: build_modality_overview(
            conn, modality, since=since, until=until, interval=interval, limit=limit))


@router.get("/admin/asset-timeline")
def asset_timeline_endpoint(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None, alias="to"),
    interval: Annotated[str, Depends(_infra._validated_interval)] = "day",
    group_by: str | None = Query(
        None, description="멀티시리즈 분할: modality | status | domain | file_ext(미지정=단일)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 생성 일자 추이(보완 v6·계보 timeline 과 대칭). 결정적·LLM 0·도메인 제외 없음.

    ``group_by``(modality/status/domain/file_ext) 주면 멀티시리즈, 미지정이면 단일. interval 검증은
    ``_validated_interval`` Depends(422).
    """
    if group_by is not None and group_by not in ("modality", "status", "domain", "file_ext"):
        raise HTTPException(status_code=422,
                            detail=f"group_by 는 modality|status|domain|file_ext 만: {group_by!r}")
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: asset_timeline(
            conn, since=since, until=until, interval=interval, group_by=group_by))


# 054·FR-301: 계보 현황 목록에서 자산 1건으로 드릴다운(관리자 관점). 사용자용 루트 ``GET /assets/{id}`` 와
# 동일한 ``fetch_asset_detail`` 노출 게이트(없음/비registered → 404)를 재사용한다(도메인 제외 없음·자산 데이터 노출 0).
# **라우트 순서(C8)**: 위 modality/{modality}·{asset_id}/lineage 를 먼저 선언해 이 catch-all 1세그가
# 그것들을 가리지 않게 — 그래서 이 라우트를 뒤에 둔다.
@router.get("/admin/assets/{asset_id}")
def admin_asset_detail(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """관리자 자산 1건 상세(계보 현황 드릴다운·FR-301). 노출 게이트는 ``fetch_asset_detail``
    책임(없음/비registered → None → 404·도메인 제외 없음). 조회 전용·LLM 0. clearance 로 ext_meta tier omit(042)."""
    detail = _infra._run_in_db(
        lambda conn: fetch_asset_detail(
            conn, asset_id=asset_id, clearance=principal.clearance))
    if detail is None:
        raise HTTPException(status_code=404, detail="자산을 찾을 수 없거나 노출 대상이 아님")
    return detail


@router.get("/admin/dashboard/summary")
def dashboard_summary_endpoint(
    months: int = Query(6, ge=1, le=24, description="월별 시계열 창(개월·기본 6·최대 24)"),
    monthly_interval: str = Query(
        "day", description="월별 슬라이스 버킷 단위: day(기본·하위호환) | month(057 FR-303·프론트 롤업 제거)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """운영 대시보드 집계 1회 응답(013 운영 후속·052 번들) — access·lineage·asset 3도메인 ×
    전체/오늘/월별/시간별. 조회 전용·결정적·LLM 0·마이그레이션 0·도메인 제외 없음.

    ``monthly_interval``(057 FR-303) — 월 범위엔 day|month 만 유효(hour 부적합) → 그 외 422.
    (TIMELINE 화이트리스트와 다른 별도 검증이라 ``_validated_interval`` 미적용.)
    """
    if monthly_interval not in ("day", "month"):
        raise HTTPException(status_code=422,
                            detail=f"monthly_interval 은 day|month 만 허용: {monthly_interval!r}")
    now = datetime.now(timezone.utc)
    return _infra._run_in_db(
        lambda conn: build_dashboard_summary(
            conn, now=now, months=months, monthly_interval=monthly_interval))


@router.get("/admin/relations/proposed-summary")
def relations_proposed_summary_endpoint(
    from_: str | None = Query(None, alias="from", description="발생일 하한(YYYY-MM-DD 또는 ISO)"),
    to: str | None = Query(None, alias="to", description="발생일 상한(exclusive)"),
    interval: Annotated[str, Depends(_infra._validated_interval)] = "day",
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """관계 제안(relations.proposed) distinct 자산 수 + 발생 추이 1회 응답(057 FR-204). 조회 전용·도메인 제외 없음·LLM 0.

    ``COUNT(DISTINCT)`` 전기간 집계(``relation_proposed_summary``·lineage occurred_at). interval 검증은
    ``_validated_interval`` Depends(422).
    """
    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    return _infra._run_in_db(
        lambda conn: relation_proposed_summary(conn, since=since, until=until, interval=interval))


@router.get("/admin/relations")
def relations_list(
    status: str = Query("proposed", description="검토 상태: proposed(큐) | active(승인) | rejected(비승인)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(
        None, max_length=200,
        description="통합 텍스트 검색(edge_id·asset_id·파일명·reason·topic·최대 200자·FR-702)"),
    asset_id: str | None = Query(None, description="양끝 중 하나 정확 일치"),
    kind_code: str | None = Query(None, description="관계종류 코드 정확 일치"),
    modality: str | None = Query(None, description="양끝 중 하나 모달리티"),
    min_confidence: float | None = Query(None, description="신뢰도 하한(≥·0~1)"),
    max_confidence: float | None = Query(None, description="신뢰도 상한(≤·0~1)"),
    reviewed_by: str | None = Query(None, description="검토자 정확 일치"),
    from_: str | None = Query(None, alias="from", description="기간 시작(inclusive·ISO)"),
    to: str | None = Query(None, description="기간 끝(exclusive·ISO)"),
    date_on: str | None = Query(None, description="기간 대상 컬럼: created | reviewed"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """관계 검토 큐/내역을 status별 페이징 조회한다(FR-101/102/103 + G7 검색·필터·기간). 조회 전용·LLM 0.

    ``status`` 화이트리스트(``_REVIEW_STATUSES``) 위반은 400. (도메인 제외 없음·2026-07-23 — 의료 엣지 포함·복귀 시 재도입.)
    G7 선택 필터: min>max·conf∉[0,1]·date_on∉{created,reviewed}·from>to → 400, 형식오류 → 422(_parse_dt).
    date_col 결정: date_on 명시 시 매핑, 생략 시 status별 자동(proposed→created_at·그외→reviewed_at).
    """
    if status not in _REVIEW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 status: {status!r} (허용: {list(_REVIEW_STATUSES)})",
        )
    for name, val in (("min_confidence", min_confidence), ("max_confidence", max_confidence)):
        if val is not None and not (0.0 <= val <= 1.0):
            raise HTTPException(status_code=400, detail=f"{name} 는 0~1 범위여야 함: {val}")
    if (min_confidence is not None and max_confidence is not None
            and min_confidence > max_confidence):
        raise HTTPException(
            status_code=400,
            detail=f"min_confidence({min_confidence}) > max_confidence({max_confidence})")

    _DATE_ON_MAP = {"created": "created_at", "reviewed": "reviewed_at"}
    if date_on is not None:
        if date_on not in _DATE_ON_MAP:
            raise HTTPException(
                status_code=400,
                detail=f"알 수 없는 date_on: {date_on!r} (허용: {list(_DATE_ON_MAP)})")
        date_col = _DATE_ON_MAP[date_on]
    else:
        date_col = "created_at" if status == "proposed" else "reviewed_at"

    since, until = _infra._parse_dt(from_), _infra._parse_dt(to)
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=400, detail=f"from({from_}) > to({to})")

    q_clean = q.strip() if q else None
    q_clean = q_clean or None

    return _infra._run_in_db(
        lambda conn: list_edges_for_review(
            conn, status=status, limit=limit, offset=offset,
            q=q_clean, asset_id=asset_id, kind_code=kind_code, modality=modality,
            min_confidence=min_confidence, max_confidence=max_confidence,
            reviewed_by=reviewed_by, since=since, until=until, date_col=date_col)
    )


@router.get("/admin/relation-kinds")
def relation_kinds_list(
    status: str | None = Query(None, description="관계종류 상태: active | inactive(생략=전체)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """관계종류 목록을 조회한다(FR-801·필터 드롭다운용·조회 전용·LLM 0).

    ``{rows:[{kind_code, kind_name_ko, status}], total}`` 를 kind_code 오름차순(결정적)으로 반환한다.
    ``status`` 화이트리스트(``_RELATION_KIND_STATUSES``) 위반은 400. RBAC = ``require_principal``.
    """
    if status is not None and status not in _RELATION_KIND_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 status: {status!r} (허용: {list(_RELATION_KIND_STATUSES)})",
        )
    return _infra._run_in_db(lambda conn: list_relation_kinds(conn, status=status))
