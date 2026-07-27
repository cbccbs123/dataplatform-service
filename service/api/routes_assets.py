"""사용자용 자산 라우트 — 상세 조회·주제 탐색·원본 다운로드·썸네일·묶음 내려받기.

**흐름에서의 위치**: 포탈 화면이 직접 부르는 경로들이다. 조회는 포탈 함수에 위임하고, 파일
전송만 이 층에서 스트리밍으로 처리한다.

⚠️ **라우트 선언 순서가 동작을 가른다.** ``/assets/unclassified`` 처럼 고정된 경로를
``/assets/{asset_id}`` 보다 **먼저** 선언해야 한다 — 뒤에 두면 "unclassified" 가 자산 id 로
해석돼 영영 404 가 된다.
"""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Iterator
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from service.api import _infra
from src.config.filename_util import display_file_name
from service.portal.asset_detail import fetch_asset_detail
from service.portal.auth import Principal, require_principal
from service.portal.download import (
    build_bundle_zip_stream,
    collect_bundle_assets,
    parse_range_header,
    resolve_download_target,
)
from service.portal.thumbnail import THUMBNAILABLE_MODALITIES, cached_thumbnail
from src.topic.asset_topic_query import (
    assets_in_topic,
    assets_unclassified,
    fetch_asset_topic,
    find_same_topic_groups,
    list_topics,
)

router = APIRouter()

# 다운로드 스트리밍 청크 크기(64KiB) — 대용량 멀티모달 자산을 메모리에 다 올리지 않는다.
_STREAM_CHUNK = 64 * 1024


@router.get("/assets/unclassified")
def unclassified_assets(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """주제가 붙지 않은 자산을 페이징해 돌려준다 — 탐색 화면의 '미분류' 폴더.

    주제 트리(``/topics``)는 ``asset_topic`` 조인이라 주제 정본이 없는 자산(분류 실패·무내용)을 누락한다.
    자산을 '빠짐없이' 보이려면 이 엔드포인트로 미분류를 회수한다. 조회 전용·도메인 제외 없음·LLM 0.
    **라우트 순서**: ``/assets/{asset_id}`` catch-all 보다 먼저 등록해야 'unclassified' 가 asset_id 로
    오매칭되지 않는다(이 위치 유지).
    """
    return _infra._run_in_db(lambda conn: assets_unclassified(conn, limit=limit, offset=offset))


@router.get("/assets/{asset_id}")
def asset_detail(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 1건 상세 — 메타·임베딩 요약·관계 미니뷰·자기주제.

    노출 여부 판정은 ``fetch_asset_detail`` 이 맡는다 — 없거나 등록 완료가 아니면 404.
    노출을 통과한 자산에는 주제 정보(``topics``·``same_topic_groups``)를 같은 읽기
    트랜잭션에서 함께 싣는다(신규 LLM 0). 게이트 미통과(None)면 주제 seam 미호출.
    """

    def _work(conn: Any) -> dict[str, Any] | None:
        """한 트랜잭션에서 상세·주제·관계를 모아 온다.

        ``None`` 은 "없거나 볼 권한이 없음" — 호출부가 404 로 바꾼다(존재 여부를 응답으로
        흘리지 않기 위해 둘을 같게 다룬다).
        """
        detail = fetch_asset_detail(conn, asset_id=asset_id, clearance=principal.clearance)
        if detail is None:
            return None
        detail["topics"] = fetch_asset_topic(conn, asset_id=asset_id)
        detail["same_topic_groups"] = find_same_topic_groups(conn, asset_id=asset_id)
        return detail

    detail = _infra._run_in_db(_work)
    if detail is None:
        raise HTTPException(status_code=404, detail="자산을 찾을 수 없거나 노출 대상이 아님")
    return detail


@router.get("/topics")
def topics_list(
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """주제 목록을 2단계(대주제 → 세부주제)로, 주제별 자산 수와 함께 돌려준다(조회 전용).

    ``list_topics`` 가 자기주제 정본(``asset_topic``·도메인 제외 없음)의 ``(topic_ko, subtopic_ko)`` 별 distinct
    정렬을 고정해(대주제 → 세부주제 이름순) 같은 요청이 늘 같은 순서를 낸다. 각 행에
    ``topic_asset_count``(주제 전체 distinct 자산 수) 동반(하위호환 필드).
    """
    return {"topics": _infra._run_in_db(list_topics)}


@router.get("/topics/{topic}")
def topic_assets(
    topic: str,
    subtopic: str | None = Query(None, description="세부주제(주면 topic 하위로 좁힘·정확 일치)"),
    unassigned: bool = Query(
        False, description="'기타'(subtopic 미부여)만 — 값 매칭이 아닌 subtopic IS NULL"
    ),
    modality: str | None = Query(None, description="모달리티 폴더(text/image/video/audio) 필터"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """특정 주제에 속한 자산을 페이징 조회한다(조회 전용).

    ``assets_in_topic`` 이 그 주제의 자기주제 정본(``asset_topic``) 자산을 distinct·``asset_id asc`` 결정적
    정렬로 페이징한다. ``subtopic`` 미지정=topic 하위 전체·``unassigned=true``='기타'(IS NULL)만·
    ``modality`` 필터. 응답 ``modality_counts`` 는 필터 무관 전체 분포(모달리티 폴더 카운트).
    """
    return _infra._run_in_db(
        lambda conn: assets_in_topic(
            conn,
            topic_ko=topic,
            subtopic_ko=subtopic,
            unassigned_only=unassigned,
            modality=modality,
            limit=limit,
            offset=offset,
        )
    )


def _guess_content_type(file_name: str, modality: str | None) -> str:
    """내려줄 파일의 MIME 타입을 정한다.

    Args:
        file_name: 확장자를 볼 파일명.
        modality: 확장자로 못 알아냈을 때 쓸 단서. ``None`` 이어도 된다.

    Returns:
        MIME 문자열. 끝까지 모르면 ``application/octet-stream``(브라우저가 열지 않고 저장한다).
    """
    ctype, _ = mimetypes.guess_type(file_name)
    if ctype:
        return ctype
    fallback = {"text": "text/plain; charset=utf-8"}
    return fallback.get(modality or "", "application/octet-stream")


def _content_disposition(file_name: str) -> str:
    """RFC 6266 attachment 헤더(ASCII filename + UTF-8 filename* 병기).

    ⚠️ ASCII 쪽에서 큰따옴표·개행·비-ASCII 를 **반드시 제거한다** — 그대로 두면 헤더가 쪼개져
    응답 위조에 쓰일 수 있다. 한글 파일명은 UTF-8 쪽에 인코딩해 함께 싣는다.

    Args:
        file_name: 내려받을 때 보일 파일명(경로가 아니라 이름만).

    Returns:
        ``Content-Disposition`` 헤더 값.
    """
    ascii_safe = "".join(c for c in file_name if c.isascii() and c.isprintable() and c != '"')
    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{quote(file_name)}'


def _file_iterator(path: str, start: int, end: int) -> Iterator[bytes]:
    """파일의 특정 구간을 조각내어 흘려보낸다.

    Args:
        path: 읽을 파일 경로.
        start: 시작 바이트(**포함**).
        end: 끝 바이트(**포함**) — 구간 요청 규격이 양 끝을 포함하므로 길이는 ``end-start+1`` 이다.

    Yields:
        바이트 조각. 파일이 도중에 짧아지면 거기서 멈춘다(예외를 올리지 않는다).
    """
    remaining = end - start + 1
    with open(path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/assets/{asset_id}/download")
def download(
    asset_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> StreamingResponse:
    """자산 원본을 스트리밍한다 — 구간 요청(이어받기·영상 탐색)을 지원한다.

    1. 노출 대상인지 먼저 확인한다 — 아니면 404.
    2. 원본 파일이 실제로 있는지 확인 — 없거나 못 읽으면 410(자산 기록은 있으나 파일이 사라진 상태).
    3. ``Range`` 헤더 있으면 ``parse_range_header`` 로 구간 산출 → 206 + ``Content-Range``; 범위 위반 → 416.
    바이트 산출은 디스크 실제 크기 기준. ``Accept-Ranges: bytes`` 항상 고지.
    """
    target = _infra._run_in_db(lambda conn: resolve_download_target(conn, asset_id=asset_id))
    if target is None:
        raise HTTPException(status_code=404, detail="다운로드 대상을 찾을 수 없거나 노출 대상이 아님")

    fs_path = target.get("fs_path")
    if not fs_path or not os.path.isfile(fs_path):
        raise HTTPException(status_code=410, detail="원본 파일이 존재하지 않거나 접근할 수 없음")

    file_size = os.path.getsize(fs_path)
    file_name = target.get("file_name") or display_file_name(fs_path)
    content_type = _guess_content_type(file_name, target.get("modality"))

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(file_name),
    }

    # 구간 계산 기준은 DB 에 적힌 크기가 아니라 **디스크 실제 크기**다 — 둘이 어긋나면
    # 있지도 않은 구간을 약속하게 된다.
    range_value = request.headers.get("range")
    try:
        rng = parse_range_header(range_value, file_size)
    except ValueError as exc:
        raise HTTPException(
            status_code=416,
            detail=f"요청 범위 충족 불가: {exc}",
            headers={"Content-Range": f"bytes */{file_size}"},
        ) from exc

    # 구간 요청이 아니면 전체를 200 으로, 맞으면 부분 응답 206 으로 — 클라이언트는 이
    # 코드로 이어받기 성공 여부를 판단한다.
    if rng is None:
        start, end, status_code = 0, file_size - 1, 200
    else:
        start, end = rng
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    # 양 끝을 포함하는 구간이라 길이는 +1 이다(빼먹으면 마지막 1바이트가 잘린다).
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        _file_iterator(fs_path, start, end),
        status_code=status_code,
        media_type=content_type,
        headers=headers,
    )


@router.get("/assets/{asset_id}/thumbnail")
def asset_thumbnail(
    asset_id: str,
    size: str = Query("card", description="크기 프리셋: card(320·목록/hover 기본) | detail(640·상세 히어로)"),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> Response:
    """이미지·영상의 축소 썸네일을 돌려준다(조회 전용).

    1. 노출 대상인지 확인 — 아니면 404.
    2. 이미지·영상이 아니면 404(오디오/텍스트/unknown 은 시각 표현 없음).
    3. 원본이 없으면 410, 썸네일 생성에 실패하면 404.
    ``cached_thumbnail`` 은 디스크 캐시 경유(generate-once·크기별) — 첫 요청만 생성·저장, 이후 캐시 서빙.
    원본 무수정·결정적·LLM 0.
    """
    target = _infra._run_in_db(lambda conn: resolve_download_target(conn, asset_id=asset_id))
    if target is None:
        raise HTTPException(status_code=404, detail="썸네일 대상을 찾을 수 없거나 노출 대상이 아님")
    modality = target.get("modality")
    if modality not in THUMBNAILABLE_MODALITIES:
        raise HTTPException(status_code=404, detail="썸네일을 제공하지 않는 자산 유형")
    fs_path = target.get("fs_path")
    if not fs_path or not os.path.isfile(fs_path):
        raise HTTPException(status_code=410, detail="원본 파일이 존재하지 않거나 접근할 수 없음")
    data = cached_thumbnail(asset_id, fs_path, modality, size=size)  # 디스크 캐시 경유(크기별 generate-once)
    if data is None:
        raise HTTPException(status_code=404, detail="썸네일을 생성할 수 없음")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/assets/{asset_id}/bundle")
def bundle(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> Response:
    """기준 자산과 **직접 연결된 이웃들**을 한 zip 으로 묶어 내려준다.

    seed 는 ``resolve_download_target`` 로 게이팅한다 — None(없음/비registered) → 404.
    이웃 자산도 ``collect_bundle_assets`` 의 SQL(registered)로 비registered 를 제외한다
    (``download.py`` 의 묶음 조회 SQL).
    """

    def _work(conn: Any) -> list[dict[str, Any]] | None:
        """묶음에 담을 자산들을 모은다.

        기준 자산이 노출 대상이 아니면 ``None`` — 호출부가 404 로 바꾼다. 이 확인을 먼저
        하지 않으면 볼 수 없는 자산을 통해 딸린 파일들이 새어 나간다.
        """
        if resolve_download_target(conn, asset_id=asset_id) is None:
            return None
        return collect_bundle_assets(conn, seed_asset_id=asset_id)

    targets = _infra._run_in_db(_work)
    if targets is None:
        raise HTTPException(status_code=404, detail="묶음 seed 를 찾을 수 없거나 노출 대상이 아님")

    # zip 조립과 전송을 모두 스트리밍으로 한다 — 파일 읽기는 DB 트랜잭션 **밖**에서, 원본은 조각으로
    # 흘려(전량 적재 0) 메모리가 묶음 크기와 무관. 누락 파일은 부분 zip + manifest(계약 불변). Starlette
    # 는 content 파일객체를 자동 close 안 하므로 BackgroundTask 로 응답 송신 후 명시적 close(임시파일 FD 정리).
    zip_stream = build_bundle_zip_stream(targets)

    def _iter_zip() -> Iterator[bytes]:
        """zip 을 조각내어 흘려보낸다 — 묶음이 아무리 커도 메모리 사용량이 일정하다."""
        while True:
            chunk = zip_stream.read(_STREAM_CHUNK)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        _iter_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"bundle_{asset_id}.zip")},
        background=BackgroundTask(zip_stream.close),
    )
