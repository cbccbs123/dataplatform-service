"""요청마다 도는 인증 의존성 — 토큰 파싱·검증 후 요청 주체를 만든다.

**흐름에서의 위치**: 라우트가 실행되기 **전에** 돈다. 여기서 만든 요청 주체의 권한 등급으로
라우트가 응답 필드를 가린다. 등급을 실제로 적용하는 일은 이 모듈 밖(투영 계층)에서 한다.

표준 Bearer 방식이라 API 문서 화면의 Authorize 버튼과 그대로 맞물린다.
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
    """토큰을 검증해 요청 주체로 바꾼다.

    Args:
        token: Authorization 헤더에서 뽑은 토큰 문자열.

    Returns:
        검증된 요청 주체.

    Raises:
        HTTPException: 검증 실패·필수 클레임 누락 시 401. **어느 항목이 틀렸는지 알려주지
            않는다** — 세부 사유를 노출하면 토큰을 맞춰 보는 데 단서가 된다.
    """
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
    """요청 주체를 만든다 — 라우트가 이 값의 권한 등급으로 응답 필드를 가린다.

    Args:
        credentials: Bearer 자격 증명. 인증을 끈 환경에서는 없을 수 있다.

    Returns:
        요청 주체. **인증을 끈 환경에서 토큰이 없으면 익명 주체**(공개 등급)를 돌려주고,
        토큰이 있으면 켜짐/꺼짐과 무관하게 검증한다.

    Raises:
        HTTPException: 인증이 켜져 있는데 토큰이 없거나 검증에 실패하면 401.
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
    """보호 라우트에 공통으로 다는 의존성 — 검색·상세·다운로드·묶음이 함께 쓴다.

    ``get_principal`` 결과를 그대로 넘긴다. 별도 이름을 둔 이유는 "이 라우트는 인증이 필요하다"를
    선언으로 드러내기 위해서다.
    """
    return principal
