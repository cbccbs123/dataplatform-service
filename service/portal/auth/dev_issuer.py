"""dev 전용 토큰 발급 (spec 042 MVP).

``POST /auth/token`` 전용 — 비밀번호 검증 없음, 로컬 스모크용.
운영 IdP 연동 시 본 모듈·엔드포인트는 비활성화 예정.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from service.portal.auth.config import load_portal_auth_config


def issue_dev_token(*, user_id: str) -> str:
    """로컬 HS256 JWT — ``LocalHs256Verifier`` 와 secret·알고리즘 쌍을 맞춘다."""
    cfg = load_portal_auth_config()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,  # ``claims_to_principal`` 이 user_id 로 읽음
        "iat": now,
        "exp": now + timedelta(seconds=cfg.jwt_ttl_seconds),
        # MVP: roles·clearance claim 없음 — 검증 후 코드가 authorized 부여(042 2-tier).
    }
    if cfg.jwt_issuer:
        # issuer 핀이 켜져 있으면 발급 토큰도 iss 를 박아 자체 검증을 통과시킨다.
        payload["iss"] = cfg.jwt_issuer
    return jwt.encode(payload, cfg.jwt_secret, algorithm="HS256")
