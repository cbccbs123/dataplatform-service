"""개발 환경 전용 토큰 발급.

``POST /auth/token`` 전용 — 비밀번호 검증 없음, 로컬 스모크용.
운영 IdP 연동 시 본 모듈·엔드포인트는 비활성화 예정.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from service.portal.auth.config import load_portal_auth_config


def issue_dev_token(*, user_id: str) -> str:
    """로컬 스모크용 토큰을 발급한다 — 검증 쪽과 비밀값·알고리즘을 맞춰 만든다.

    Args:
        user_id: 토큰 주체. 검증 뒤 이 값이 요청자 식별자가 된다.

    Returns:
        서명된 토큰 문자열. 유효 기간과 발급자 핀은 설정에서 읽는다.
    """
    cfg = load_portal_auth_config()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,  # ``claims_to_principal`` 이 user_id 로 읽음
        "iat": now,
        "exp": now + timedelta(seconds=cfg.jwt_ttl_seconds),
        # 토큰에 역할·등급을 담지 않는다 — 검증을 통과하면 코드가 기본 등급을 부여한다.
    }
    if cfg.jwt_issuer:
        # issuer 핀이 켜져 있으면 발급 토큰도 iss 를 박아 자체 검증을 통과시킨다.
        payload["iss"] = cfg.jwt_issuer
    return jwt.encode(payload, cfg.jwt_secret, algorithm="HS256")
