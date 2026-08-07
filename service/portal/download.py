"""다운로드 지원 — 단일 자산 원본과 관계 묶음(zip).

**흐름에서의 위치**: 라우트가 파일을 내려보내기 직전에 쓰는 조회·조립 계층이다. 구간 헤더
파싱만 순수 함수이고, 나머지는 DB 를 읽어 대상과 경로를 확정한다. **쓰기는 없다**(헌법 6조).

**결과가 매번 같아야 한다**(헌법 3조) — 묶음 순서도 zip 엔트리 순서·타임스탬프도 고정한다.
같은 요청이 다른 바이트를 내면 비교도 캐시도 무의미해진다.

관계는 반드시 그래프 읽기 통로를 거친다 — 직접 쿼리하면 대칭 엣지의 절반을 놓친다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

# 묶음 이웃은 반드시 graph_query seam 경유(대칭 엣지 양방향·status 필터). 직접 graph_edge 쿼리 금지.
from src.config.filename_util import (
    display_file_name,  # 내려받을 때 보일 파일명 — 저장 시 붙은 id 접두를 떼어 준다
)
from src.relations.graph_query import fetch_active_relations_for_asset

logger = logging.getLogger(__name__)

_BYTES_PREFIX = "bytes="

# 노출 기준은 상세 조회와 같다 — 등록 완료 자산만. 그 외는 404(없는 것과 같게 다룬다).
_DOWNLOAD_TARGET_SQL = """
SELECT asset_id, fs_path, fs_uri, file_size, modality, domain_label, status
FROM asset
WHERE asset_id = %s
LIMIT 1
"""

# 묶음에 담을 이웃들의 파일 경로 조회.
# seed 는 ``resolve_download_target`` 로 registered 게이트됨 — 이웃만 SQL 로 재필터.
# 아직 등록이 끝나지 않은 이웃은 묶음에서 빠진다(파일이 제자리에 없을 수 있다).
_BUNDLE_PATHS_SQL = """
SELECT asset_id, fs_path FROM asset
WHERE asset_id = ANY(%s)
  AND status = 'registered'
"""


def parse_range_header(range_value: str | None, file_size: int) -> tuple[int, int] | None:
    """HTTP ``Range`` 헤더를 ``(start, end)`` 바이트 오프셋(둘 다 포함)으로 파싱한다(순수).

    지원 형식(단일 범위만):
        - ``bytes=start-end`` → ``(start, end)``
        - ``bytes=start-``    → ``(start, file_size-1)`` (열린 끝)
        - ``bytes=-suffix``   → ``(file_size-suffix, file_size-1)`` (마지막 suffix 바이트,
          suffix 가 파일보다 크면 전체로 클램프 — RFC 7233)
    헤더가 ``None`` 이면 ``None``(=전체 다운로드). 끝이 파일 크기 이상이면 ``file_size-1`` 로
    **클램프**한다(RFC 7233 §2.1 — 거부 아님; 일부 플레이어/다운로드 매니저가 stale 한 큰 end 를
    재요청해도 206 응답). 시작이 파일 크기 이상·역순·형식 오류·다중 범위는 ``ValueError`` 로
    거부한다(API 가 416 으로 응답). 범위가 파일 끝을 넘으면 **거부하지 않고 끝까지로 줄인다** —
    표준이 그렇게 정하고 있고, 엄격히 거부하면 이어받기 클라이언트가 실패한다.

    Args:
        range_value: ``Range`` 헤더 값. ``None`` 이면 전체 다운로드라는 뜻이다.
        file_size: 대상 파일 크기(클램프 기준).

    Returns:
        ``(start, end)`` — **둘 다 포함**하는 구간. 헤더가 없으면 ``None``.

    Raises:
        ValueError: 시작이 파일 크기 이상 · 역순 · 형식 오류 · 다중 범위. 호출부가 416 으로 바꾼다.
    """
    if range_value is None:
        return None

    text = range_value.strip()
    if not text.startswith(_BYTES_PREFIX):
        raise ValueError(f"지원하지 않는 Range 단위: {range_value!r}")
    spec = text[len(_BYTES_PREFIX):].strip()

    # 다중 범위(콤마)는 본 MVP 미지원 — 단일 범위만 처리.
    if "," in spec:
        raise ValueError("다중 Range 는 미지원(단일 범위만)")
    if "-" not in spec:
        raise ValueError(f"Range 형식 오류: {range_value!r}")

    start_str, end_str = (part.strip() for part in spec.split("-", 1))
    try:
        if start_str == "":
            # 접미 형식 bytes=-suffix : 마지막 suffix 바이트.
            if end_str == "":
                raise ValueError("Range 형식 오류(빈 범위)")
            suffix = int(end_str)
            if suffix <= 0:
                raise ValueError("Range suffix 는 양수여야 함")
            start = max(0, file_size - suffix)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str != "" else file_size - 1
    except ValueError as exc:
        # int 변환 실패도 형식 오류로 통일(416 의미는 아래 범위 검증에서).
        raise ValueError(f"Range 형식 오류: {range_value!r} ({exc})") from exc

    if start < 0:
        raise ValueError(f"Range 시작이 음수: {range_value!r}")
    if start >= file_size:
        raise ValueError(f"Range 시작이 파일 크기 이상(416): {start} >= {file_size}")
    if end < start:
        raise ValueError(f"Range 역순(416): {start} > {end}")
    # 끝이 파일 크기 이상이면 거부하지 않고 마지막 바이트로 클램프한다(RFC 7233 §2.1).
    if end >= file_size:
        end = file_size - 1
    return (start, end)


def resolve_download_target(
    conn: Connection[Any], *, asset_id: str
) -> dict[str, Any] | None:
    """``asset_id`` 의 단일 다운로드 타깃을 해소한다(노출 가능 여부까지 여기서 판단).

    Returns:
        registered 면 ``{"asset_id","fs_path","fs_uri","file_size","modality","file_name"}``
        (``file_name`` = ``fs_path`` basename). 행 없음/비registered → ``None`` (API 404).

    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DOWNLOAD_TARGET_SQL, (asset_id,))
        row = cur.fetchone()

    if row is None:
        return None
    if row["status"] != "registered":
        return None

    fs_path = row["fs_path"]
    return {
        "asset_id": str(row["asset_id"]),
        "fs_path": fs_path,
        "fs_uri": row["fs_uri"],
        "file_size": row["file_size"],
        "modality": row["modality"],
        "file_name": display_file_name(fs_path),
    }


def _fetch_asset_paths(
    conn: Connection[Any], asset_ids: list[str]
) -> dict[str, Any]:
    """자산 id 들의 파일 경로를 **한 번에** 조회한다(자산마다 따로 묻지 않는다).

    Args:
        asset_ids: 조회할 자산 목록. 빈 목록이면 DB 를 건드리지 않는다.

    Returns:
        ``{asset_id: fs_path}``. **경로가 없는 자산은 빠진다** — 호출부가 그것으로 묶음에서
        제외할지 판단한다.
    """
    if not asset_ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_BUNDLE_PATHS_SQL, (list(asset_ids),))
        rows = cur.fetchall()
    return {str(r["asset_id"]): r["fs_path"] for r in rows}


def collect_bundle_assets(
    conn: Connection[Any],
    *,
    seed_asset_id: str,
    max_neighbors: int = 50,
    min_confidence: float = 0.0,
) -> list[dict[str, Any]]:
    """기준 자산과 **직접 연결된 이웃**을 묶음 대상으로 모은다.

    절차
        1. ``graph_query``(active, **양방향**)로 이웃을 받아 self-loop 제외·``min_confidence``
           미만 제외·중복 이웃(다중 엣지)은 최고 confidence 로 합친다.
        2. 결정적 정렬 — 이웃을 (confidence desc, asset_id asc)로 줄세운 뒤 ``max_neighbors``
           초과 시 상위 N 으로 절단(허브 자산 메가블롭 방지, 절단 시 ``log`` 경고).
        3. seed 를 맨 앞에 두고 각 자산의 ``fs_path``/``file_name`` 을 채워 반환.

    Args:
        conn: 열려 있는 연결.
        seed_asset_id: 기준 자산.
        max_neighbors: 담을 이웃 수 상한. **연결이 아주 많은 자산 때문에 묶음이 통째로
            거대해지는 것을 막는 안전장치** — 잘리면 경고를 남긴다.
        min_confidence: 이 값 미만인 관계는 버린다. 기본값 0 은 전부 통과다.

    Returns:
        ``[{"asset_id","fs_path","file_name"}, ...]`` — 기준 자산 먼저, 그다음 이웃(순서 고정).
        이웃이 없으면 기준 자산 하나만 담는다. 경로를 모르는 항목은 뺀다.
    """
    seed_id = str(seed_asset_id)
    # 🔴 **`active`(확인된 관계) 만 담는 것은 의도된 것이다** — 상세 화면을 따라 바꾸지 말 것.
    #
    # 081 항목6 에서 상세 화면(`asset_detail`)은 확인 전(`proposed`)까지 보여주도록 바꿨지만,
    # **묶음은 바꾸지 않기로 결정했다**(사용자 2026-08-07). 보는 것과 내보내는 것은 무게가
    # 다르다 — 화면의 오표시는 사용자가 넘기면 끝나지만, 틀린 파일이 담긴 zip 은 메일·보고서·
    # 타 시스템으로 퍼지고 **회수할 수 없다**. 확인 전 구간의 오류율이 낮지 않다
    # (`duplicate_near` 0.7 = 48.3% 오분류) — 그런 자산을 함께 내보내면 절반이 오배포다.
    #
    # ⚠️ 지금은 `active` 가 0건이라 **묶음이 사실상 기준 자산 1개**다. 이는 묶음 정책의 결함이
    # 아니라 **사람 승인이 0건**인 상태의 반영이며, 승인이 시작되면 자연히 채워진다.
    # 되돌리기 쉬운 쪽을 먼저 골랐다 — 나중에 넣는 건 한 줄, 나간 zip 회수는 불가능하다.
    # 근거: `docs/설계_변경이력.md`(2026-08-07) · spec 081 항목5.
    neighbors = fetch_active_relations_for_asset(conn, asset_id=seed_id)

    # 이웃 dedup: 같은 자산이 여러 엣지로 와도 한 번만(최고 confidence 보존). None → 0.0 정화.
    best_conf: dict[str, float] = {}
    for n in neighbors:
        nid = str(n["asset_id"])
        if nid == seed_id:
            continue  # self-loop 방어
        conf = n.get("confidence")
        conf = float(conf) if conf is not None else 0.0
        if conf < min_confidence:
            continue
        if nid not in best_conf or conf > best_conf[nid]:
            best_conf[nid] = conf

    # 결정적 정렬: confidence desc, asset_id asc.
    ordered = sorted(best_conf.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) > max_neighbors:
        logger.warning(
            "묶음 이웃 %d개가 max_neighbors=%d 초과 — confidence 상위 %d개로 절단(seed=%s)",
            len(ordered), max_neighbors, max_neighbors, seed_id,
        )
        ordered = ordered[:max_neighbors]

    # 최종 순서: seed 먼저 → 이웃(위 정렬). 경로 일괄 조회 후 enrich.
    ordered_ids = [seed_id] + [nid for nid, _ in ordered]
    path_map = _fetch_asset_paths(conn, ordered_ids)

    targets: list[dict[str, Any]] = []
    for aid in ordered_ids:
        fs_path = path_map.get(aid)
        if fs_path is None:
            continue  # asset 행 없음(파일 경로 미상) → 묶음 제외
        targets.append(
            {"asset_id": aid, "fs_path": fs_path, "file_name": display_file_name(fs_path)}
        )
    return targets


def _dedup_entry_name(name: str, used: set[str]) -> str:
    """zip 안에서 이름이 겹치면 번호를 붙여 유일하게 만든다.

    같은 이름으로 두 번 넣으면 압축 해제 때 하나가 덮어써진다.

    Args:
        name: 넣으려는 이름.
        used: **이미 쓴 이름 집합** — 이 함수가 여기에 결과를 추가한다(호출자와 공유하는 상태).

    Returns:
        충돌하지 않는 이름.
    """
    if name not in used:
        used.add(name)
        return name
    root, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = f"{root}_{i}{ext}"
        if cand not in used:
            used.add(cand)
            return cand
        i += 1


# 복사 조각 크기 — 원본을 통째로 메모리에 올리지 않기 위한 값.
_BUNDLE_COPY_CHUNK = 64 * 1024
# zip 결과가 이 크기를 넘으면 메모리 대신 디스크 임시파일로 굴린다(요청당 메모리 상한).
_BUNDLE_SPOOL_MAX = 64 * 1024 * 1024


def build_bundle_zip_stream(targets: list[dict[str, Any]]) -> tempfile.SpooledTemporaryFile:
    """묶음 대상들을 zip **스트림**으로 만든다 — 묶음이 커도 메모리 사용량이 일정하다.

    기존 ``build_bundle_zip``(bytes)이 파일마다 ``fh.read()`` 전체 적재 + 전체 zip 을 BytesIO 로
    들고 있어 대용량 묶음에서 OOM/DoS 소지가 있었다. 원본→zip 엔트리를 ``copyfileobj``(64KiB)로
    흘리고, 결과도 ``SpooledTemporaryFile``(64MiB 초과 시 디스크)로 받아 메모리가 묶음 크기와
    무관해진다. 반환 파일은 위치 0 으로 되감아 주며, 호출자가 소비 후 닫는다(StreamingResponse
    는 응답 종료 시 자동 close).

    **파일이 없어도 실패하지 않는다** — 빠진 것은 건너뛰고 목록 파일에 적어 넣는다(하나 때문에
    묶음 전체가 실패하면 안 되기 때문). 엔트리 순서와 타임스탬프를 고정해, 같은 입력이면
    **바이트까지 같은 zip** 이 나온다.

    Args:
        targets: 담을 자산 목록(순서가 곧 zip 엔트리 순서).

    Returns:
        위치 0 으로 되감긴 임시 파일. 일정 크기를 넘으면 자동으로 디스크로 넘어간다.
    """
    spool = tempfile.SpooledTemporaryFile(max_size=_BUNDLE_SPOOL_MAX)
    missing: list[dict[str, Any]] = []
    used_names: set[str] = set()

    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            for t in targets:
                fs_path = t.get("fs_path")
                file_name = t.get("file_name") or display_file_name(fs_path)
                try:
                    fh = open(fs_path, "rb")  # noqa: SIM115 — copy 루프와 수명 분리(아래 with 로 닫음)
                except (OSError, TypeError):
                    missing.append(
                        {"asset_id": t.get("asset_id"), "fs_path": fs_path, "file_name": file_name}
                    )
                    continue
                entry = _dedup_entry_name(file_name, used_names)
                info = zipfile.ZipInfo(filename=entry, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                # zlib 스트리밍 압축(zf.open+copyfileobj)은 단일콜(writestr)과 청크 경계 무관하게
                # **바이트 동일**(리뷰서 64KiB 초과 데이터로 검증) — 결정성·기존 zip 바이트 보존.
                with fh, zf.open(info, "w") as dst:
                    shutil.copyfileobj(fh, dst, _BUNDLE_COPY_CHUNK)

            if missing:
                manifest = json.dumps(
                    {"missing": missing}, ensure_ascii=False, sort_keys=True, indent=2
                )
                _write_zip_entry(zf, "_manifest.json", manifest.encode("utf-8"))
    except BaseException:
        # 중간에 실패해도 임시 파일 핸들이 새지 않게 닫고 예외를 다시 올린다
        # (64MiB 초과분은 실제 디스크 임시파일이라 FD 누수 실해가 있다). 성공 경로만 열어서 반환.
        spool.close()
        raise

    spool.seek(0)
    return spool


def build_bundle_zip(targets: list[dict[str, Any]]) -> bytes:
    """완성된 zip 을 통째로 메모리에 올려 돌려주는 래퍼.

    ⚠️ **운영 경로에서는 쓰지 않는다** — 크기만큼 메모리를 먹는다. zip 내용을 직접 열어
    확인해야 하는 테스트·소규모 호출용으로만 남겨 둔다. 실제 응답은 스트리밍 쪽을 쓴다.

    Args:
        targets: 담을 자산 목록(순서가 곧 zip 엔트리 순서).

    Returns:
        zip 전체 바이트.
    """
    with build_bundle_zip_stream(targets) as spool:
        return spool.read()


def _write_zip_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    """zip 엔트리 하나를 쓴다 — **타임스탬프를 고정**해 같은 입력이면 같은 바이트가 나오게.

    현재 시각을 넣으면 내용이 같아도 파일이 매번 달라져 비교·캐시가 안 된다.

    Args:
        zf: 대상 zip.
        arcname: zip 안에서의 경로.
        data: 넣을 바이트.
    """
    info = zipfile.ZipInfo(filename=arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)
