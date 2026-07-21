"""요청 주체(Principal) — 검증기와 분리된 애플리케이션 계약 (spec 042).

포탈·``project_ext_meta`` 는 JWT 세부가 아니라 ``Principal``(user_id·clearance) 만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.registry.access_tier import principal_clearance


@dataclass(frozen=True)
class Principal:
    """인증(또는 anonymous) 요청 주체."""

    user_id: str
    clearance: str  # ``project_ext_meta`` 키 제거(omit) 판정 입력


ANONYMOUS = Principal(
    user_id="anonymous",
    clearance=principal_clearance(authenticated=False),
)


def claims_to_principal(claims: dict[str, Any]) -> Principal:
    """검증된 JWT claims → ``Principal``.

    MVP: ``sub`` → user_id, 유효 토큰이면 clearance ``authorized`` (2-tier MVP).
    clearance 는 JWT payload 에 넣지 않음 — 검증 후 코드에서 부여.
    """
    user_id = claims.get("sub")
    if not user_id or not isinstance(user_id, str):
        raise ValueError("토큰 subject 누락")
    return Principal(user_id=user_id, clearance=principal_clearance(authenticated=True))
