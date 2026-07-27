"""사람이 관계를 검토하는 라우트 — 승인·반려·정정·종류 승격, 그리고 감사 기록.

**흐름에서의 위치**: 이 패키지에서 **유일하게 DB 에 쓰는** 라우트다. 나머지는 전부 조회다.
판단 로직은 코어 검토 함수가 갖고, 여기서는 HTTP 로 노출하고 감사를 붙이는 일만 한다.

**결정과 감사를 한 트랜잭션에 묶는다** — 결정만 남고 누가 했는지가 빠지면 되돌릴 근거가 없다.
다만 감사 기록 자체가 실패해도 결정은 살린다(기록 때문에 사람의 결정이 날아가면 안 된다).

검토자는 인증된 사용자 id 를 그대로 쓴다 — 요청 본문에서 받지 않는다(위조 여지를 없앤다).
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
    """일괄 승인·반려 요청 — 화면에서 고른 엣지 id 목록만 받는다(전체 선택 같은 암묵 대상 없음)."""

    edge_ids: list[str]


class RelationReviseRequest(BaseModel):
    """결정 정정 요청 — 이미 내린 판단을 사람이 되돌릴 때만 쓴다."""

    edge_id: str
    to_status: str


def _record_relation_audit(conn: Any, *, action: str, reviewer: str, detail: dict) -> None:
    """결정과 **같은 트랜잭션**에 감사 기록을 남긴다.

    ``psycopg`` 의 중첩 ``conn.transaction()`` 은 SAVEPOINT 다 — 감사 INSERT 가 실패해도 savepoint 만
    롤백돼 바깥 결정 트랜잭션(approve/reject/revise/promote 갱신)은 보존된다(감사 best-effort·결정 무손상).
    ``detail`` 은 jsonb 로 edge_id/kind_code 를 담고, ``access_log.asset_id`` 는 관계에 부적합하므로 NULL.
    미들웨어는 GET 조회만 감사하므로 이 쓰기 요청이 이중으로 기록되지는 않는다.

    Args:
        action: 감사에 남길 동작 이름(``relation.approve`` 등).
        reviewer: 결정을 내린 사람.
        detail: 무엇을 결정했는지(엣지 id·종류 코드 등). 자산 단위가 아니라 관계 단위라
            ``asset_id`` 는 비워 둔다.
    """
    try:
        with conn.transaction():
            record_access(conn, action=action, user_id=reviewer, detail=detail)
    except Exception:  # noqa: BLE001 — 감사 실패가 결정을 되돌리면 안 된다(최선 노력)
        _LOG.warning("relation 감사 기록 실패(무시): %s %s", action, detail)


def _bulk_decide(action: str, edge_ids: list[str], reviewer: str) -> dict[str, Any]:
    """일괄 승인·반려 공통 처리 — 결정과 감사를 한 트랜잭션에서 수행한다.

    실제로 바뀐 건(``ok=True``)만 감사에 남긴다 — 이미 결정돼 있어 아무 일도 안 한 건까지
    기록하면 감사 로그가 실제 변경과 어긋난다.

    Args:
        action: ``approve`` 또는 ``reject``.
        edge_ids: 처리할 엣지 목록. **비어 있으면 400** — 아무 대상도 없는 요청은 오작동 신호다.
        reviewer: 결정자.

    Returns:
        ``{results: [{edge_id, ok}]}``. ``ok=False`` 는 없거나 이미 결정된 건이다.

    Raises:
        HTTPException: 빈 목록이면 400.
    """
    if not edge_ids:
        raise HTTPException(status_code=400, detail="edge_ids 는 1개 이상이어야 함")

    def _work(conn: Any) -> dict[str, Any]:
        """검토 결정과 감사 기록을 **한 트랜잭션에서** 처리한다.

        결정만 반영되고 감사 기록이 빠지면 누가 승인했는지 추적할 수 없으므로 함께 묶는다.
        """
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
    """검토 대기 엣지를 일괄 승인한다 — 건별 성공 여부를 배열로 돌려준다.

    Args:
        body: 승인할 엣지 id 목록.
        principal: 인증된 요청 주체. 이 사람이 검토자로 기록된다.

    Returns:
        ``{results: [{edge_id, ok}]}``. **이미 결정된 엣지는 예외가 아니라 ``ok=False``** —
        나머지 건의 처리를 멈추지 않기 위해서다.
    """
    return _bulk_decide("approve", body.edge_ids, principal.user_id)


@router.post("/admin/relations/reject")
def relations_reject(
    body: RelationDecisionRequest,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """검토 대기 엣지를 일괄 반려한다 — 건별 성공 여부를 배열로 돌려준다.

    **행을 지우지 않고 상태만 바꾼다** — 기록이 남아 있어야 이후 LLM 이 같은 관계를 다시
    제안해도 '반려됨'이 덮이지 않는다.

    Args:
        body: 반려할 엣지 id 목록.
        principal: 인증된 요청 주체(검토자로 기록).

    Returns:
        ``{results: [{edge_id, ok}]}``. 이미 결정된 엣지는 예외가 아니라 ``ok=False``.
    """
    return _bulk_decide("reject", body.edge_ids, principal.user_id)


@router.post("/admin/relations/revise")
def relations_revise(
    body: RelationReviseRequest,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """이미 내린 결정을 정정한다 — 검토 대기 가드를 우회하는 유일한 경로다.

    승인·반려는 '아직 결정 안 된 것'만 건드리지만, 이 경로는 **이미 결정된 것도 바꾼다** —
    잘못 누른 결정을 되돌릴 유일한 수단이라서다.

    Args:
        body: 대상 엣지와 바꿀 상태.
        principal: 인증된 요청 주체(정정자로 감사에 남는다).

    Returns:
        ``{results: [{edge_id, ok}]}`` — 단건이어도 배열로 감싸 일괄 처리와 응답 모양을 맞춘다.

    Raises:
        HTTPException: 허용 목록 밖 상태면 400.
    """
    if body.to_status not in _REVIEW_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 to_status: {body.to_status!r} (허용: {list(_REVIEW_STATUSES)})",
        )
    reviewer = principal.user_id

    def _work(conn: Any) -> dict[str, Any]:
        """결정 정정과 감사 기록을 한 트랜잭션에서 처리한다."""
        ok = revise_edge(conn, edge_id=body.edge_id, reviewer=reviewer, to_status=body.to_status)
        if ok:
            _record_relation_audit(
                conn, action="relation.revise", reviewer=reviewer,
                detail={"edge_id": body.edge_id, "to_status": body.to_status})
        # 응답 모양을 일괄 처리와 통일한다 — 단건이어도 배열로 감싼다(화면 분기 제거).
        return {"results": [{"edge_id": body.edge_id, "ok": ok}]}

    return _infra._run_in_db_write(_work)


@router.post("/admin/relation-kinds/{kind_code}/promote")
def relation_kind_promote(
    kind_code: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """검토 대기 상태인 관계 종류를 승격해 실제로 쓰이게 한다.

    여러 번 눌러도 안전하다 — 이미 승격된 종류면 아무 일도 하지 않고 ``ok=False`` 만 돌려준다.

    Args:
        kind_code: 승격할 관계 종류 코드.
        principal: 인증된 요청 주체. **감사 기록에만 남는다** — 관계 종류 테이블에는
            검토자 컬럼이 없다.

    Returns:
        ``{kind_code, ok}``. ``ok=False`` 는 없거나 이미 승격된 종류다.
    """
    reviewer = principal.user_id

    def _work(conn: Any) -> dict[str, Any]:
        """관계 종류 승격과 감사 기록을 한 트랜잭션에서 처리한다."""
        ok = promote_relation_kind(conn, kind_code=kind_code, reviewer=reviewer)
        if ok:
            _record_relation_audit(
                conn, action="relation.kind_promote", reviewer=reviewer,
                detail={"kind_code": kind_code})
        return {"kind_code": kind_code, "ok": ok}

    return _infra._run_in_db_write(_work)
