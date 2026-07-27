"""액세스 토큰 검증.

``TokenVerifier`` Protocol — backend 교체 시 ``verify`` 구현만 추가.
현재: ``LocalHs256Verifier`` (dev ``/auth/token`` 과 동일 secret·HS256).

발급은 ``dev_issuer`` — 본 모듈은 **검증만**.
"""

from __future__ import annotations

from typing import Any, Protocol

import jwt

from service.portal.auth.config import PortalAuthConfig, load_portal_auth_config


class TokenVerifier(Protocol):
    """Bearer access token 검증. 성공 시 검증된 claims dict."""

    def verify(self, token: str) -> dict[str, Any]:
        """토큰을 검증하고 담긴 정보를 돌려준다. **검증 실패는 예외**(빈 dict 아님)."""
        ...


class LocalHs256Verifier:
    """dev/MVP — 포탈 자체 HS256(secret 공유)."""

    def __init__(self, config: PortalAuthConfig) -> None:
        """설정에서 비밀키와 발급자를 받아 둔다(발급자가 없으면 그 항목은 검사하지 않는다)."""
        self._secret = config.jwt_secret
        self._issuer = config.jwt_issuer  # None 이면 iss 미검사(단일 secret MVP).

    def verify(self, token: str) -> dict[str, Any]:
        """토큰을 검증한다 — 만료 시각과 주체가 **반드시** 있어야 한다.

        만료가 없는 토큰은 영구 토큰이 되고, 주체가 없으면 누구의 요청인지 기록할 수 없다.
        발급자를 설정해 두면 그것까지 확인해, 같은 비밀키를 쓰는 다른 서비스의 토큰이
        여기서 통하는 것을 막는다.

        Returns:
            검증된 클레임 dict.

        Raises:
            토큰 라이브러리의 검증 예외를 그대로 올린다 — 호출부가 401 로 변환한다.
        """
        required = ["exp", "sub"]
        decode_kwargs: dict[str, Any] = {}
        if self._issuer:
            required.append("iss")
            decode_kwargs["issuer"] = self._issuer
        return jwt.decode(
            token,
            self._secret,
            algorithms=["HS256"],
            options={"require": required},
            **decode_kwargs,
        )


_verifier: TokenVerifier | None = None  # 프로세스 내 싱글턴 — secret·backend 변경은 재기동 전제.


def get_token_verifier(*, config: PortalAuthConfig | None = None) -> TokenVerifier:
    """``PortalAuthConfig.backend`` 에 맞는 검증기 싱글턴.

    무인자 호출은 캐시된 ``_verifier`` 를 재사용한다(없으면 env 설정으로 1회 생성). 단 ``config=`` 를
    명시하면 항상 새로 만들어 **전역 ``_verifier`` 를 덮어쓴다** — 이후의 무인자 호출도 그 검증기를
    받는 부수효과가 있다(요청별 config 주입 API 가 아니라 전역 교체). 테스트는 이 오염을 막으려
    ``_reset_verifier_for_tests`` 로 캐시를 비운 뒤 원하는 config 로 세팅한다.

    Args:
        config: 설정을 직접 주입할 때만 준다. **주면 전역 싱글턴이 교체된다** —
            요청마다 다른 설정을 쓰라고 만든 통로가 아니다.

    Returns:
        설정에 맞는 검증기.

    Raises:
        ValueError: 아직 구현하지 않은 백엔드 종류일 때.
    """
    global _verifier
    if _verifier is not None and config is None:
        return _verifier
    cfg = config or load_portal_auth_config()
    if cfg.backend == "local_hs256":
        _verifier = LocalHs256Verifier(cfg)
        return _verifier
    raise ValueError(f"미구현 auth backend: {cfg.backend!r}")


def _reset_verifier_for_tests() -> None:
    """단위 테스트용 — 검증기 캐시 초기화."""
    global _verifier
    _verifier = None
