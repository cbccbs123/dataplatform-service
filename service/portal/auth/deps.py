"""FastAPI Depends — Bearer 파싱·검증·Principal (spec 042).

``HTTPBearer``(``portal_bearer_scheme``) — OpenAPI ``/docs`` 상단 Authorize 버튼.
curl·프론트는 기존과 동일하게 ``Authorization: Bearer <JWT>`` 헤더를 쓴다.
런타임 clearance·``project_ext_meta`` 집행 로직은 본 모듈 밖으로 새지 않는다.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from service.portal.auth.config import load_portal_auth_config
from service.portal.auth.principal import ANONYMOUS, Principal, claims_to_principal
from service.portal.auth.verifier import get_token_verifier

# auto_error=False — PORTAL_AUTH_DISABLED=1 일 때 토큰 없이 anonymous 허용(401 아님).
portal_bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "JWT access token. ``POST /auth/token`` 으로 발급. "
        "값에는 **토큰 문자열만** 입력(Bearer 접두사 불필요)."
    ),
)


def authenticate_token(token: str) -> Principal:
    """검증기 + ``claims_to_principal``. 실패 시 HTTP 401."""
    try:
        claims = get_token_verifier().verify(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰") from exc
    try:
        return claims_to_principal(claims)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def get_principal(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(portal_bearer_scheme)
    ] = None,
) -> Principal:
    """보호 라우트 principal — clearance 가 ``project_ext_meta`` 키 제거 판정에 쓰인다.

    ``PORTAL_AUTH_DISABLED=1``: 토큰 없음 → anonymous(public), Bearer 있으면 검증.
    비활성 아님: Bearer 필수.
    """
    cfg = load_portal_auth_config()
    token = credentials.credentials if credentials else None
    if cfg.auth_disabled:
        if token:
            return authenticate_token(token)
        return ANONYMOUS
    if not token:
        raise HTTPException(status_code=401, detail="인증 필요")
    return authenticate_token(token)


def require_principal(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    """보호 라우트용 Depends 래퍼 — ``portal_api`` 검색·상세·다운로드·묶음에 공통 배선."""
    return principal
