"""포탈 앱 인프라 (069 US-E FR-E6·A) — DB 앱수명 풀·트랜잭션 seam·lifespan·접근이력 미들웨어.

종전 ``portal_api.py`` 상단의 앱 인프라 코드를 **그대로 이관**한다(로직 불변). 별도 모듈로 두는 이유는
**순환 import 회피**다 — 라우터(``routes_*``)가 ``_run_in_db``/``_validated_interval`` 을 쓰는데, 이를
``__init__`` 에 두면 ``__init__``(라우터 include) ↔ 라우터가 서로를 import 하는 순환이 생긴다. ``_infra``
는 아무 라우터도 import 하지 않으므로(routes→_infra, __init__→routes+_infra) 순환이 없다.

``app`` 자체(및 미들웨어·예외핸들러·lifespan 등록)는 ``__init__`` 이 소유한다 — 여기서는 그 **함수/상태**
만 제공하고 ``__init__`` 이 ``app`` 에 배선한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from service.portal._timeline_util import TIMELINE_INTERVALS
from service.portal.access_log import derive_access_action, record_access
from service.portal.auth import authenticate_token

_ENV = os.getenv("PORTAL_API_ENV", "dev")

# access_log 적재 실패(best-effort)·미들웨어 진단용 모듈 로거(서비스 응답엔 영향 없음).
_LOG = logging.getLogger("meta_extract.portal_api")


# 069 P1-1: 앱 수명 PostgresUtil 싱글턴 — 기존엔 요청(트랜잭션)마다 PostgresUtil()+`with db:` 로
# **풀을 생성·즉시 파괴**해 풀링 이득이 0(매 요청 TCP+auth 재수행·고부하 시 커넥션 폭증)이었다.
# 지연 생성(최초 요청)·프로세스 공유·lifespan 종료 시 close. 생성 경합은 락으로 차단.
_DB_SINGLETON: object | None = None
_DB_LOCK = threading.Lock()


def _get_db() -> object:
    """앱 수명 DB 싱글턴을 돌려준다(최초 호출 시 풀 1회 생성 — 069 P1-1)."""
    global _DB_SINGLETON
    # 이중 검사 락(double-checked locking): 락 밖 첫 검사로 이미 생성된 정상 경로의 락 경합을 피하고,
    # 락 안에서 다시 검사해 경쟁 스레드가 풀을 중복 생성(2개)하지 않게 한다.
    if _DB_SINGLETON is None:
        with _DB_LOCK:
            if _DB_SINGLETON is None:
                from src.database.postgres_util import PostgresUtil

                db = PostgresUtil()
                db.open_pool()
                _DB_SINGLETON = db
    return _DB_SINGLETON


def _close_db_singleton() -> None:
    """lifespan 종료 훅 — 싱글턴 풀을 닫고 초기화한다(재기동·테스트 격리용)."""
    global _DB_SINGLETON
    if _DB_SINGLETON is not None:
        try:
            _DB_SINGLETON.close()
        except Exception:  # noqa: BLE001 — 종료 경로 best-effort(닫힘 실패가 셧다운을 막지 않게)
            _LOG.warning("DB 풀 close 실패(무시)", exc_info=True)
        _DB_SINGLETON = None


def _run_in_db(callback: Callable[[object], object]) -> object:
    """PostgresUtil 조회 트랜잭션에서 ``callback(conn)`` 을 실행하는 단일 seam.

    상세/다운로드/묶음 핸들러의 DB 접근은 모두 이 함수를 거친다(테스트는 이 함수를 patch 로
    대체해 DB 없이 단위 검증). ``idempotent=True`` 조회 전용(쓰기 0, 헌법 6조).
    069 P1-1: 요청당 풀 생성·파괴 대신 앱 수명 싱글턴(``_get_db``)을 재사용한다.
    """
    return _get_db().execute_in_transaction(callback, idempotent=True)


def _run_in_db_write(callback: Callable[[object], object]) -> object:
    """조회 seam(``_run_in_db``)과 분리한 write 트랜잭션(``idempotent=False``·commit) 공유 seam.

    원본 자산 payload·스키마는 무변경이나, 두 부류의 거버넌스 write 가 이 seam 을 공유한다:
    (1) 미들웨어의 append-only 감사(``access_log`` 1행·013 FR-012·best-effort),
    (2) 052 관계 검토 결정(``bulk_review``/``revise_edge``/``promote_relation_kind`` — graph_edge
    status·relation_kind status 전이 + relation 감사). 테스트는 이 함수를 patch.
    069 P1-1: 앱 수명 싱글턴 재사용.
    """
    return _get_db().execute_in_transaction(callback, idempotent=False)


def _parse_dt(value: str | None) -> datetime | None:
    """``YYYY-MM-DD`` 또는 ISO datetime 문자열을 ``datetime`` 으로 파싱한다.

    빈 값은 ``None``(필터 비활성). 형식 오류는 ``HTTPException(422)`` 로 거부한다.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"날짜 형식 오류: {value!r}") from exc


def _validated_interval(
    interval: str = Query("day", description="집계 단위: hour | day | week | month"),
) -> str:
    """timeline 계열 라우트의 interval 화이트리스트 검증 Depends(069 FR-E6).

    종전 4개 핸들러가 각자 ``if interval not in TIMELINE_INTERVALS: raise HTTPException(422, …)`` 을
    복제하던 것을 단일 의존성으로 통합한다(동작 동일 — 같은 화이트리스트·같은 422). 유효하면 그대로
    반환해 핸들러가 주입받는다.
    """
    if interval not in TIMELINE_INTERVALS:
        raise HTTPException(
            status_code=422,
            detail=f"interval 은 {'|'.join(TIMELINE_INTERVALS)} 만 허용: {interval!r}",
        )
    return interval


def _user_id_from_request(request: Request) -> str:
    """best-effort: ``Authorization: Bearer <token>`` → user_id. 없거나 검증 실패면 ``anonymous``.

    기록(감사) 용 식별이라 인증 실패가 응답을 막아선 안 된다 — 어떤 예외든 삼키고 anonymous 로.
    실제 접근 인가는 라우트의 ``require_principal`` 이 이미 책임진다(여기선 기록 라벨링만).
    미들웨어는 라우트 의존성 주입 전에 돌아 route 의 Principal 을 못 받으므로 토큰을 여기서 다시 파싱한다.
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            return authenticate_token(auth[7:].strip()).user_id
        except Exception:  # noqa: BLE001 — 기록용 best-effort, 인증 실패가 응답을 막지 않음
            return "anonymous"
    return "anonymous"


def _record_access_safe(method: str, path: str, status_code: int, user_id: str) -> None:
    """데이터 접근(성공 응답)을 ``access_log`` 에 1행 적재. 비대상·오류 응답은 무시(best-effort).

    4xx/5xx 응답은 기록하지 않고, ``derive_access_action`` 이 데이터 라우트로 판정한 GET 만
    append-only 로 적재한다(검색·상세·다운로드·묶음). 그 외(감사 뷰·health 등)는 None → skip.
    """
    if status_code >= 400:
        return
    derived = derive_access_action(method, path)
    if derived is None:
        return
    action, asset_id = derived
    _run_in_db_write(
        lambda conn: record_access(conn, action=action, user_id=user_id, asset_id=asset_id)
    )


# fire-and-forget 기록 태스크 강참조 보관(GC 로 중도 소멸 방지). 완료 시 자동 제거.
_PENDING_TASKS: set[asyncio.Task] = set()


async def _record_access_bg(method: str, path: str, status_code: int, user_id: str) -> None:
    """동기 DB write 를 스레드풀에서 수행하는 비차단 기록 태스크. 어떤 예외도 삼킨다(best-effort)."""
    try:
        await run_in_threadpool(_record_access_safe, method, path, status_code, user_id)
    except Exception:  # noqa: BLE001 — 감사 기록 실패가 서비스에 전파되면 안 됨(best-effort·D2)
        _LOG.warning("access_log 기록 실패(무시): %s %s", method, path)


async def access_log_middleware(request: Request, call_next: Callable) -> object:
    """데이터 접근 이력을 append-only 로 적재한다(013 US3·FR-008).

    기록을 **응답 critical path 에서 분리**(fire-and-forget)한다 — 응답을 먼저 반환하고 기록은
    ``create_task`` 로 뒤에서 수행한다. 동기 DB write 를 await 하면 DB 지연/풀 고갈 시 모든 데이터
    응답이 지연되므로(best-effort 감사가 서비스 지연을 유발), await 하지 않는다(D2). 기록 실패·지연은
    응답 상태·지연 어디에도 영향이 없다. 응답 객체는 변경 없이 그대로 반환.
    """
    response = await call_next(request)
    try:
        user_id = _user_id_from_request(request)
        task = asyncio.create_task(
            _record_access_bg(request.method, request.url.path, response.status_code, user_id)
        )
        _PENDING_TASKS.add(task)
        task.add_done_callback(_PENDING_TASKS.discard)
    except Exception:  # noqa: BLE001 — 기록 스케줄 실패조차 응답을 깨면 안 됨(best-effort)
        _LOG.warning("access_log 기록 스케줄 실패(무시): %s %s", request.method, request.url.path)
    return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 부트스트랩: 백엔드 자기 레포 루트의 .env.{env} 로드 → 코어 init_settings(필수 env 검증). 1회만.
    # 코어 src.config.bootstrap 이 아니라 백엔드 전용(service.bootstrap)을 쓴다 — 코어 것은 코어 레포 .env 를
    # 로드하기 때문(레포 분리 077: 백엔드는 dataplatform-service/.env 를 로드해야 함).
    from service.bootstrap import bootstrap_env

    bootstrap_env(_ENV)
    yield
    # graceful shutdown: 남은 fire-and-forget 감사 기록 태스크를 드레인한다(best-effort·013 FR-012).
    if _PENDING_TASKS:
        await asyncio.gather(*_PENDING_TASKS, return_exceptions=True)
    # 069 P1-1: 앱 수명 DB 풀 정리(드레인 뒤 — 감사 write 가 풀을 쓸 수 있으므로 순서 중요).
    _close_db_singleton()


# 069 P1-4(권고): OS 연결 실패(인프라 다운·타임아웃)를 코드버그 500 과 구분해 **503** 으로 —
# 운영 알람·관측 구분용(ConnectionTimeout 은 ConnectionError 하위라 함께 잡힘). opensearchpy 는
# 검색 백엔드(037) 필수 의존이나, 부분 설치 환경에서도 포탈 기동이 죽지 않게 지연·방어 임포트.
try:
    from opensearchpy.exceptions import ConnectionError as OSConnectionError
except ImportError:  # 미설치 환경 방어(검색 요청 시점에 별도 ImportError 로 드러남)
    OSConnectionError = None  # type: ignore[assignment,misc]


async def os_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """OpenSearch 연결 실패 → 503(코드버그 500 과 구분·운영 알람용). __init__ 이 app 에 등록."""
    _LOG.warning("OpenSearch 연결 실패(503 반환): %s %s — %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "검색 엔진(OpenSearch) 연결 실패 — 잠시 후 다시 시도해 주세요."},
    )
