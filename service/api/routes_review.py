"""HITL 관계 검토 라우트 (069 US-E FR-E6·A) — 052 승인/반려/정정/승격 write 결정 + 감사.

종전 ``portal_api.py`` 의 ``/admin/relations/{approve,reject,revise}``·``/admin/relation-kinds/{code}/promote``
POST 핸들러를 그대로 이관한다(동작 불변). review.py 의 검증된 단일 트랜잭션 로직을 HTTP 로 노출하는 thin
레이어다. write 3종은 결정+감사를 한 write 트랜잭션(``_infra._run_in_db_write``)에 묶고, 감사 실패는
savepoint 로 결정을 보존한다(best-effort·FR-502). RBAC = require_principal(현 2-tier MVP·reviewer=user_id).

테스트 patch 정본: ``patch("service.api.routes_review.<name>")``(bulk_review·revise_edge·
promote_relation_kind·record_access — 쓰는 곳에서 대체).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from service.api import _infra
from service.portal.access_log import record_access
from service.portal.auth import Principal, require_principal
from src.relations.review import (
    _REVIEW_STATUSES,
    bulk_review,
    promote_relation_kind,
    revise_edge,
)

router = APIRouter()

_LOG = _infra._LOG


class RelationDecisionRequest(BaseModel):
    """일괄 승인/반려 요청 — UI 체크박스로 고른 edge_id 목록(C3·명시 목록만)."""

    edge_ids: list[str]


class RelationReviseRequest(BaseModel):
    """결정 정정 요청 — 사람 전용 status 전이(C4)."""

    edge_id: str
    to_status: str


def _record_relation_audit(conn: Any, *, action: str, reviewer: str, detail: dict) -> None:
    """결정과 **같은 write 트랜잭션**에 감사(access_log)를 기록한다(FR-203/502·D5).

    ``psycopg`` 의 중첩 ``conn.transaction()`` 은 SAVEPOINT 다 — 감사 INSERT 가 실패해도 savepoint 만
    롤백돼 바깥 결정 트랜잭션(approve/reject/revise/promote 갱신)은 보존된다(감사 best-effort·결정 무손상).
    ``detail`` 은 jsonb 로 edge_id/kind_code 를 담고, ``access_log.asset_id`` 는 관계에 부적합하므로 NULL.
    미들웨어 ``derive_access_action`` 은 GET 데이터 라우트만 판정하므로 이 POST 는 이중 기록 없다.
    """
    try:
        with conn.transaction():
            record_access(conn, action=action, user_id=reviewer, detail=detail)
    except Exception:  # noqa: BLE001 — 감사 실패가 결정 트랜잭션을 깨지 않음(best-effort·FR-502)
        _LOG.warning("relation 감사 기록 실패(무시): %s %s", action, detail)


def _bulk_decide(action: str, edge_ids: list[str], reviewer: str) -> dict[str, Any]:
    """일괄 승인/반려 공통 — 결정+감사를 한 write 트랜잭션에서 수행(FR-203/502).

    빈 목록은 400(의미 없는 요청·오작동 방지). ``bulk_review`` per-id 결과를 받아 ``ok=True`` 건만
    ``relation.{action}`` 감사를 같은 트랜잭션에 남긴다(ok=False 는 미기록).
    """
    if not edge_ids:
        raise HTTPException(status_code=400, detail="edge_ids 는 1개 이상이어야 함")

    def _work(conn: Any) -> dict[str, Any]:
        results = bulk_review(conn, edge_ids=edge_ids, reviewer=reviewer, action=action)
        for r in results:
            if r["ok"]:
                _record_relation_audit(
                    conn, action=f"relation.{action}", reviewer=reviewer,
                    detail={"edge_id": r["edge_id"]})
        return {"results": results}

    return _infra._run_in_db_write(_work)


@router.post("/admin/relations/approve")
def relations_approve(
    body: RelationDecisionRequest,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """proposed 엣지 일괄 승인(→active)·per-id 결과·감사(FR-201/203/502·US2). LLM 0.

    reviewer = ``principal.user_id``. 이미 결정된 엣지는 ``ok=False`` 로 반환(예외 아님).
    """
    return _bulk_decide("approve", body.edge_ids, principal.user_id)


@router.post("/admin/relations/reject")
def relations_reject(
    body: RelationDecisionRequest,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """proposed 엣지 일괄 반려(→rejected)·per-id 결과·감사(FR-202/203/502·US2). LLM 0.

    소프트 반려(행 보존·status 전이만) — 이후 LLM 재제안이 status 를 덮지 않아 rejected 보존.
    reviewer = ``principal.user_id``.
    """
    return _bulk_decide("reject", body.edge_ids, principal.user_id)


@router.post("/admin/relations/revise")
def relations_revise(
    body: RelationReviseRequest,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """결정 정정(사람 전용·proposed 가드 없음)·감사(FR-301/302/502·US4). LLM 0.

    ``to_status`` 화이트리스트(``_REVIEW_STATUSES``) 위반은 400. active↔rejected·→proposed 전 방향 전이
    허용(오결정 되돌림·C4). reviewer = ``principal.user_id``.
    """
    if body.to_status not in _REVIEW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 to_status: {body.to_status!r} (허용: {list(_REVIEW_STATUSES)})",
        )
    reviewer = principal.user_id

    def _work(conn: Any) -> dict[str, Any]:
        ok = revise_edge(conn, edge_id=body.edge_id, reviewer=reviewer, to_status=body.to_status)
        if ok:
            _record_relation_audit(
                conn, action="relation.revise", reviewer=reviewer,
                detail={"edge_id": body.edge_id, "to_status": body.to_status})
        # 055 FR-201: approve/reject 와 동일 봉투 {results:[{edge_id,ok}]} 로 통일(단건도 배열).
        return {"results": [{"edge_id": body.edge_id, "ok": ok}]}

    return _infra._run_in_db_write(_work)


@router.post("/admin/relation-kinds/{kind_code}/promote")
def relation_kind_promote(
    kind_code: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """inactive relation_kind 를 active 로 승격(어휘 거버넌스)·감사(FR-401/502·US5). LLM 0.

    기존 ``promote_relation_kind``(inactive 가드·멱등) 재사용 — 이미 active 면 ``ok=False``.
    reviewer(``principal.user_id``)는 감사에만 남는다(relation_kind 에 reviewed_by 컬럼 없음).
    """
    reviewer = principal.user_id

    def _work(conn: Any) -> dict[str, Any]:
        ok = promote_relation_kind(conn, kind_code=kind_code, reviewer=reviewer)
        if ok:
            _record_relation_audit(
                conn, action="relation.kind_promote", reviewer=reviewer,
                detail={"kind_code": kind_code})
        return {"kind_code": kind_code, "ok": ok}

    return _infra._run_in_db_write(_work)
