"""포탈 API 요청 스키마 — OpenAPI/Swagger 입력 폼용 (spec 042).

런타임 검증만 담당. clearance·JWT 서명 등 인증 정책은 ``deps``·``config`` 가 집행.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DevTokenRequest(BaseModel):
    """``POST /auth/token`` body — dev JWT 발급(``PORTAL_AUTH_DISABLED=1`` 일 때만).

    ``user_id``·``username`` 둘 다 있으면 ``user_id`` 우선(나머지 하나만 쓰는 일반 케이스는 동일).
    """

    username: str | None = Field(
        default=None,
        description="발급 user_id(``user_id`` 미지정 시 사용, 둘 다 없으면 dev-user)",
        examples=["dev-user"],
    )
    user_id: str | None = Field(
        default=None,
        description="발급 user_id(``username`` 보다 우선)",
        examples=["alice"],
    )
