"""일반 도메인 포탈 API 패키지 (069 US-E FR-E6·A) — FastAPI ``app`` 조립·라우터 포함.

종전 단일 ``portal_api.py``(1689줄)를 책임별로 분할했다: 인프라(``_infra``)·검색(``routes_search``)·
사용자 자산(``routes_assets``)·관리자 조회(``routes_admin``)·관계 검토 write(``routes_review``). 이
``__init__`` 만 ``app`` 을 소유하고, lifespan·접근이력 미들웨어·OS 예외핸들러를 ``_infra`` 에서 가져와
배선하며, 4개 라우터를 **원래 등록 순서**로 include 한다.

하위호환: 진입점·테스트는 ``from service.api import app`` 을 쓰는데, ``portal_api.py`` 가 이
패키지의 ``app`` 을 재export 하는 얇은 심으로 남는다.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException

from service.api import _infra, routes_admin, routes_assets, routes_review, routes_search
from service.portal.auth import Principal, require_principal
from service.portal.auth.config import load_portal_auth_config
from service.portal.auth.dev_issuer import issue_dev_token
from service.portal.auth.schemas import DevTokenRequest

app = FastAPI(title="일반 도메인 포탈 API (010 P1)", lifespan=_infra.lifespan)

# 접근이력 append-only 미들웨어(013 US3·FR-008) — _infra 가 fire-and-forget 로직·상태를 소유, 여기서 배선.
app.middleware("http")(_infra.access_log_middleware)

# 069 P1-4: OpenSearch 연결 실패 → 503(코드버그 500 과 구분·운영 알람). opensearchpy 미설치 환경은 skip.
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
