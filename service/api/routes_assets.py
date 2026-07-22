"""사용자 자산 라우트 (069 US-E FR-E6·A) — 자산 상세·주제 브라우즈·원본 다운로드·썸네일·번들.

종전 ``portal_api.py`` 의 사용자용(루트) 자산 핸들러를 그대로 이관한다(동작 불변). 서비스 helper는 홈에서
직접 import(테스트 patch 정본=``service.api.routes_assets.<name>``), 인프라(``_run_in_db``)는 ``_infra``
모듈참조. **라우트 순서**: ``/assets/unclassified``·``/topics`` 를 catch-all ``/assets/{asset_id}``·
``/topics/{topic}`` 보다 먼저 선언(구체 경로 우선).
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
    """주제 미부여('미분류') 자산 페이징 — 자산목록 파일탐색기의 최상위 '미분류' 폴더(070).

    주제 트리(``/topics``)는 ``asset_topic`` 조인이라 주제 정본이 없는 자산(분류 실패·무내용)을 누락한다.
    자산을 '빠짐없이' 보이려면 이 엔드포인트로 미분류를 회수한다. 조회 전용·의료 제외·LLM 0.
    **라우트 순서**: ``/assets/{asset_id}`` catch-all 보다 먼저 등록해야 'unclassified' 가 asset_id 로
    오매칭되지 않는다(이 위치 유지).
    """
    return _infra._run_in_db(lambda conn: assets_unclassified(conn, limit=limit, offset=offset))


@router.get("/assets/{asset_id}")
def asset_detail(
    asset_id: str,
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """자산 1건 상세(메타·임베딩 채널 요약·관계 미니뷰)를 반환한다(FR-004/005/006 + 056 FR-501).

    노출 게이트(FR-014)는 ``fetch_asset_detail`` 이 책임진다 — 없음/비registered/의료면 None → 404.
    065 FR-402: 노출 통과 자산에 자기주제 정본 렌즈(``topics``·``same_topic_groups``)를 같은 읽기
    트랜잭션에서 함께 싣는다(신규 LLM 0). 게이트 미통과(None)면 주제 seam 미호출.
    """

    def _work(conn: Any) -> dict[str, Any] | None:
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
    """주제 목록(topic→subtopic 2단계·주제별 자산 수)을 반환한다(065 FR-402·US2). 조회 전용·LLM 0.

    ``list_topics`` 가 자기주제 정본(``asset_topic``·의료 제외)의 ``(topic_ko, subtopic_ko)`` 별 distinct
    자산 수를 결정적 정렬(topic_ko asc→subtopic_ko asc)로 집계한다. 057 FR-105: 각 행에
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
    """특정 주제에 속한 자산을 페이징 조회한다(065 FR-402·US2). 조회 전용·의료 제외·LLM 0.

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
    """파일명 확장자 → MIME, 실패 시 모달리티 기반 폴백(최종 octet-stream)."""
    ctype, _ = mimetypes.guess_type(file_name)
    if ctype:
        return ctype
    fallback = {"text": "text/plain; charset=utf-8"}
    return fallback.get(modality or "", "application/octet-stream")


def _content_disposition(file_name: str) -> str:
    """RFC 6266 attachment 헤더(ASCII filename + UTF-8 filename* 병기).

    ASCII fallback 에서 큰따옴표·제어문자(CR/LF)·비-ASCII 를 제거해 헤더 분리/인젝션을 막는다
    (UTF-8 ``filename*`` 측은 ``quote`` 로 안전). file_name 은 basename 이라 현실 위험은 낮으나 위생.
    """
    ascii_safe = "".join(c for c in file_name if c.isascii() and c.isprintable() and c != '"')
    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{quote(file_name)}'


def _file_iterator(path: str, start: int, end: int) -> Iterator[bytes]:
    """``[start, end]`` (둘 다 포함) 구간을 청크 단위로 읽어 흘려보낸다(메모리 절약·스트리밍)."""
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
    """단일 자산 원본을 스트리밍한다 — HTTP ``Range`` 부분 요청(206) 지원(FR-007/009).

    1. ``resolve_download_target`` 노출 게이트(registered·비의료) 통과 → None → 404.
    2. 원본 파일 존재 확인 → 없거나 접근 불가면 410(FR-009).
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

    range_value = request.headers.get("range")
    try:
        rng = parse_range_header(range_value, file_size)
    except ValueError as exc:
        raise HTTPException(
            status_code=416,
            detail=f"요청 범위 충족 불가: {exc}",
            headers={"Content-Range": f"bytes */{file_size}"},
        ) from exc

    if rng is None:
        start, end, status_code = 0, file_size - 1, 200
    else:
        start, end = rng
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

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
    """이미지·영상 자산의 축소 썸네일(JPEG)을 반환한다(057-후속·멀티모달 시각 미리보기). 조회 전용.

    1. ``resolve_download_target`` 노출 게이트(registered·**비의료**·FR-014) → None → 404(의료=PHI 차단).
    2. 이미지·영상이 아니면 404(오디오/텍스트/unknown 은 시각 표현 없음).
    3. 원본 부재/접근 불가 → 410(FR-009). 생성 실패 → 404.
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
    """seed 자산 기준 관계 ego-network(seed + 1-hop active 이웃)를 zip 으로 묶어 내려준다(FR-008).

    seed 는 ``resolve_download_target`` 로 게이팅한다 — None(없음/의료/비registered) → 404.
    이웃 자산도 ``collect_bundle_assets`` 의 SQL(``domain_label <> 'medical'`` + registered)로 의료·비registered
    를 제외한다(PHI 이중 차단·download.py ``_BUNDLE_PATHS_SQL``). NULL domain 은 SQL 특성상 함께 제외되나,
    registered 자산은 'general' 이 부여돼 실무상 과다배제 영향은 없다(안전 방향).
    """

    def _work(conn: Any) -> list[dict[str, Any]] | None:
        # seed 게이트: 노출 불가(의료/비registered/없음) seed → None 신호 → 404.
        if resolve_download_target(conn, asset_id=asset_id) is None:
            return None
        return collect_bundle_assets(conn, seed_asset_id=asset_id)

    targets = _infra._run_in_db(_work)
    if targets is None:
        raise HTTPException(status_code=404, detail="묶음 seed 를 찾을 수 없거나 노출 대상이 아님")

    # 069 P1-2: zip 조립·응답 모두 스트리밍 — 파일 IO 는 DB 트랜잭션 밖, 원본은 64KiB 청크로 zip 에
    # 흘려(전량 적재 0) 메모리가 묶음 크기와 무관. 누락 파일은 부분 zip + manifest(계약 불변). Starlette
    # 는 content 파일객체를 자동 close 안 하므로 BackgroundTask 로 응답 송신 후 명시적 close(임시파일 FD 정리).
    zip_stream = build_bundle_zip_stream(targets)

    def _iter_zip() -> Iterator[bytes]:
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
