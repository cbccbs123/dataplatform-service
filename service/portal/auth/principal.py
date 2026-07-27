"""요청 주체 — 토큰 검증기와 분리된 애플리케이션 쪽 계약.

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
    """검증된 클레임을 요청 주체로 바꾼다.

    권한 등급은 **토큰에 담지 않고 코드가 부여한다** — 토큰에 넣으면 발급처가 등급까지 정하게 되고,
    등급 정책을 바꿀 때 이미 발급된 토큰들이 남아 버린다.

    Args:
        claims: 검증을 통과한 클레임 dict.

    Returns:
        요청 주체.

    Raises:
        ValueError: 주체(``sub``)가 없을 때 — 누구의 요청인지 모르면 감사 기록을 남길 수 없다.
    """
    user_id = claims.get("sub")
    if not user_id or not isinstance(user_id, str):
        raise ValueError("토큰 subject 누락")
    return Principal(user_id=user_id, clearance=principal_clearance(authenticated=True))
