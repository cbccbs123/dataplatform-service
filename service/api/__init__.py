"""포탈 API 패키지 — FastAPI 앱 조립과 라우터 등록.

**흐름에서의 위치**: 요청이 가장 먼저 닿는 곳이다. 여기서 앱을 만들고, 수명 주기·접근 기록
미들웨어·예외 처리기를 붙인 뒤 라우터 넷을 등록한다. 실제 처리 로직은 각 라우터 모듈에 있다.

``app`` 객체를 소유하는 곳은 **여기 하나**다 — 진입점도 테스트도 이 모듈에서 가져다 쓴다.

⚠️ **라우터 등록 순서를 바꾸지 말 것.** FastAPI 는 먼저 등록된 경로부터 맞춰 보므로, 순서가
바뀌면 어떤 경로가 다른 경로를 가려 404 가 난다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException

from service.api import _infra, routes_admin, routes_assets, routes_review, routes_search
from service.portal.auth import Principal, require_principal
from service.portal.auth.config import load_portal_auth_config
from service.portal.auth.dev_issuer import issue_dev_token
from service.portal.auth.schemas import DevTokenRequest

app = FastAPI(title="일반 도메인 포탈 API (010 P1)", lifespan=_infra.lifespan)

# 접근 기록 미들웨어 — 실제 로직·상태는 ``_infra`` 가 갖고, 여기서는 앱에 붙이기만 한다.
app.middleware("http")(_infra.access_log_middleware)

# 검색 엔진 연결 실패는 503 으로 — 코드 버그(500)와 구분해야 알람을 나눌 수 있다.
# 라이브러리가 없는 환경에서는 이 핸들러를 등록하지 않는다.
if _infra.OSConnectionError is not None:
    app.add_exception_handler(_infra.OSConnectionError, _infra.os_unavailable_handler)


# ── 메타 라우트(health/auth/me) — 앱 수준·소규모라 여기 직접 등록(원래도 맨 앞) ──────────────
@app.get("/health")
def health() -> dict[str, str]:
    """헬스 체크(부트스트랩·라우팅 확인용)."""
    return {"status": "ok", "env": _infra._ENV}


@app.post("/auth/token")
def auth_token(body: DevTokenRequest) -> dict[str, str]:
    """dev JWT 발급 — ``PORTAL_AUTH_DISABLED=1`` 일 때만. 로컬 스모크·Swagger Authorize 용.

    운영(``PORTAL_AUTH_DISABLED=0``)에서는 404 — IdP 연동 전 dev 엔드포인트 노출 방지.
    """
    if not load_portal_auth_config().auth_disabled:
        raise HTTPException(status_code=404, detail="dev 토큰 발급 비활성")
    user_id = (body.user_id or body.username or "dev-user").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="username 또는 user_id 필요")
    return {"access_token": issue_dev_token(user_id=user_id), "token_type": "bearer"}


@app.get("/me")
def me(principal: Annotated[Principal, Depends(require_principal)]) -> dict[str, str]:
    """현재 principal(user_id·clearance)."""
    return {"user_id": principal.user_id, "clearance": principal.clearance}


# ── 라우터 포함(원래 등록 순서: admin GET → review POST → search → assets) ────────────────
# 경로 공간이 겹치지 않아(/admin/*·/search·/assets/*·/topics*) 라우터 간 순서는 매칭에 무관하나,
# 원래 순서를 보존한다. catch-all 라우트 순서는 각 라우터 파일 내부에서 보장(구체 경로 먼저 선언).
app.include_router(routes_admin.router)
app.include_router(routes_review.router)
app.include_router(routes_search.router)
app.include_router(routes_assets.router)

__all__ = ["app"]
