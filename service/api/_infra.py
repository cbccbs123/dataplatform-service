"""앱 공통 인프라 — DB 풀·트랜잭션 실행 통로·수명 주기·접근 기록 미들웨어.

**흐름에서의 위치**: 라우터들이 DB 를 쓰려면 반드시 여기를 거친다. 앱 객체 자체와 미들웨어
배선은 패키지 ``__init__`` 이 소유하고, 이 모듈은 **함수와 상태만** 제공한다.

별도 모듈로 떼어 둔 이유는 **순환 import** 다 — 라우터가 쓰는 함수를 ``__init__`` 에 두면
``__init__``(라우터를 등록) ↔ 라우터(함수를 사용)가 서로를 import 하게 된다. 이 모듈은 어떤
라우터도 import 하지 않으므로 그 고리가 생기지 않는다.
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


# DB 접근 객체는 **앱 수명 동안 하나만** 둔다 — 요청마다 만들면 연결 풀이 매번 새로 생겼다 사라져
# **풀을 생성·즉시 파괴**해 풀링 이득이 0(매 요청 TCP+auth 재수행·고부하 시 커넥션 폭증)이었다.
# 지연 생성(최초 요청)·프로세스 공유·lifespan 종료 시 close. 생성 경합은 락으로 차단.
_DB_SINGLETON: object | None = None
_DB_LOCK = threading.Lock()


def _get_db() -> object:
    """앱 전체가 공유하는 DB 접근 객체를 돌려준다(첫 호출 때 연결 풀이 한 번 열린다)."""
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
    대체해 DB 없이 단위 검증). 요청마다 풀을 만들지 않고 앱 수명 객체를 재사용한다.

    Args:
        callback: 커넥션을 받아 조회를 수행하는 함수. **쓰기를 하면 안 된다** — 이 경로는
            재시도 가능한 것으로 표시돼 있어, 쓰기가 섞이면 중복 반영될 수 있다.

    Returns:
        ``callback`` 의 반환값.
    """
    return _get_db().execute_in_transaction(callback, idempotent=True)


def _run_in_db_write(callback: Callable[[object], object]) -> object:
    """조회 seam(``_run_in_db``)과 분리한 write 트랜잭션(``idempotent=False``·commit) 공유 seam.

    원본 자산 payload·스키마는 무변경이나, 두 부류의 거버넌스 write 가 이 seam 을 공유한다:
    (1) 미들웨어가 남기는 접근 기록(한 행씩 추가·실패해도 요청을 깨지 않음),
    (2) 관계 검토 결정(``bulk_review``/``revise_edge``/``promote_relation_kind`` — graph_edge
    status·relation_kind status 전이 + relation 감사). 테스트는 이 함수를 patch 한다.

    Args:
        callback: 커넥션을 받아 쓰기를 수행하는 함수.

    Returns:
        ``callback`` 의 반환값. 실패하면 트랜잭션이 통째로 롤백된다.
    """
    return _get_db().execute_in_transaction(callback, idempotent=False)


def _parse_dt(value: str | None) -> datetime | None:
    """``YYYY-MM-DD`` 또는 ISO datetime 문자열을 ``datetime`` 으로 파싱한다.

    Args:
        value: 날짜 문자열. 빈 값·``None`` 이면 필터를 걸지 않는다는 뜻이다.

    Returns:
        파싱된 ``datetime``, 또는 값이 없으면 ``None``.

    Raises:
        HTTPException: 형식이 틀렸을 때 422. **기본값으로 넘기지 않는다** — 사용자가 의도한
            기간과 다른 결과를 조용히 보여주면 안 되기 때문이다.
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
    """시계열 라우트의 버킷 단위 값을 검증한다(허용 목록 밖이면 422).

    시계열 라우트들이 공유하는 **의존성**이다 — 핸들러마다 같은 검사를 복제하면 한 곳만
    고쳐져 서로 어긋난다. 통과하면 값을 그대로 넘겨 핸들러가 주입받는다.

    Args:
        interval: 버킷 단위. 허용 목록은 시계열 공용 상수 하나뿐이다.

    Returns:
        검증을 통과한 값.

    Raises:
        HTTPException: 허용 목록 밖이면 422.
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
    """성공한 데이터 조회를 접근 이력에 한 행 남긴다.

    **DB에 쓴다.** 실패해도 예외를 올리지 않는다(감사는 최선 노력 — 기록 때문에 요청이 깨지면 안 된다).

    Args:
        method: HTTP 메서드.
        path: 요청 경로. 여기서 동작 이름과 대상 자산을 도출한다.
        status_code: 응답 코드. **400 이상이면 기록하지 않는다** — 실패한 접근은 감사 대상이
            아니다(무엇을 봤는지가 아니라 못 봤다는 뜻이므로).
        user_id: 요청 주체.
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
    """기록 작업을 **응답과 분리해** 뒤에서 수행한다(스레드풀 경유).

    동기 DB 쓰기를 응답 경로에서 기다리면 DB 가 느릴 때 모든 요청이 함께 느려진다.
    어떤 예외도 삼키고 경고만 남긴다.

    Args:
        method: HTTP 메서드.
        path: 요청 경로.
        status_code: 응답 코드.
        user_id: 요청 주체.
    """
    try:
        await run_in_threadpool(_record_access_safe, method, path, status_code, user_id)
    except Exception:  # noqa: BLE001 — 감사 기록 실패가 서비스에 전파되면 안 됨(best-effort·D2)
        _LOG.warning("access_log 기록 실패(무시): %s %s", method, path)


async def access_log_middleware(request: Request, call_next: Callable) -> object:
    """접근 이력을 **추가만** 하는 방식으로 적재한다(수정·삭제 없음 — 감사 기록이므로).

    기록을 **응답 critical path 에서 분리**(fire-and-forget)한다 — 응답을 먼저 반환하고 기록은
    ``create_task`` 로 뒤에서 수행한다. 동기 DB write 를 await 하면 DB 지연/풀 고갈 시 모든 데이터
    응답이 지연되므로(best-effort 감사가 서비스 지연을 유발), await 하지 않는다(D2). 기록 실패·지연은
    응답 상태·지연 어디에도 영향이 없다.

    Args:
        request: 들어온 요청.
        call_next: 다음 처리 단계. 이 결과를 **그대로** 돌려준다(응답을 건드리지 않는다).

    Returns:
        아래 단계가 만든 응답 객체 그대로.
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
    """앱의 수명 주기 — 기동 시 설정을 확정하고, 종료 시 남은 작업과 DB 풀을 정리한다.

    ⚠️ 부트스트랩은 **백엔드 전용**을 쓴다(코어 것이 아니라). 코어 부트스트랩은 코어 레포의
    ``.env`` 를 읽기 때문에, 그걸 쓰면 백엔드가 남의 설정으로 뜬다.

    종료 순서가 중요하다 — 감사 기록 태스크를 **먼저** 비운 뒤 DB 풀을 닫는다. 반대로 하면
    아직 쓰기 중인 태스크가 닫힌 풀을 잡는다.
    """
    from service.bootstrap import bootstrap_env

    bootstrap_env(_ENV)
    yield
    # 종료 시 남은 감사 기록 작업을 먼저 비운다(응답과 분리돼 뒤에서 돌던 것들).
    if _PENDING_TASKS:
        await asyncio.gather(*_PENDING_TASKS, return_exceptions=True)
    # 그다음에 DB 풀을 닫는다 — **순서가 중요하다**. 먼저 닫으면 아직 쓰는 중인 감사 작업이 실패한다.
    _close_db_singleton()


# 검색 엔진 연결 실패는 **503** 으로 응답한다 — 코드 버그(500)와 구분해야 운영자가
# 운영 알람·관측 구분용(ConnectionTimeout 은 ConnectionError 하위라 함께 잡힘). opensearchpy 는
# 알람을 나눌 수 있다. 라이브러리가 없는 환경에서도 앱이 뜨도록 import 는 지연·방어적으로 한다.
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
