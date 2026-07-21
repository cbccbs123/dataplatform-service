"""포탈 다운로드 지원 — 단일 자산·관계 묶음(spec 010 D-4/D-5).

본 파일은 그룹별로 함수가 늘어난다:
    - **G2**: ``parse_range_header`` — HTTP Range 헤더 파싱(**완전 순수**, DB·IO 0).
    - **G3**: ``resolve_download_target``(단일 다운로드 타깃 해소·노출 게이트) ·
      ``collect_bundle_assets``(관계 ego-network 묶음 수집·결정적 정렬) ·
      ``build_bundle_zip``(원본 → zip, 누락은 manifest 로 고지하는 부분 zip).

읽기 전용
    asset/graph 조회만(쓰기·스키마 변경 0, 헌법 6조). 관계는 ``graph_query`` seam 경유.
    결정성(헌법 3조): 묶음 정렬 tiebreak(seed→confidence→asset_id) + zip 엔트리 순서·타임스탬프 고정.
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
    display_file_name,  # 표시/다운로드 파일명 asset_id 프리픽스 제거(065 T605)
)
from src.relations.graph_query import fetch_active_relations_for_asset

logger = logging.getLogger(__name__)

_BYTES_PREFIX = "bytes="

# 다운로드 노출 게이트(상세와 동일): registered·비의료만 노출. 그 외/없음은 API 가 404.
_DOWNLOAD_TARGET_SQL = """
SELECT asset_id, fs_path, fs_uri, file_size, modality, domain_label, status
FROM asset
WHERE asset_id = %s
LIMIT 1
"""

# 묶음 이웃 경로 조회 (010 P1 · 042 FR-007).
# seed 는 ``resolve_download_target`` 로 registered·비의료 게이트됨 — 이웃만 SQL 로 재필터.
# 비registered·medical 이웃은 fs_path 없이 묶음에서 제외(의료 seed 는 상위 404).
_BUNDLE_PATHS_SQL = """
SELECT asset_id, fs_path FROM asset
WHERE asset_id = ANY(%s)
  AND status = 'registered'
  AND domain_label <> 'medical'
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
    거부한다(API 가 416 매핑). (2026-06-08 코드리뷰: plan D-4 의 strict 거부를 RFC 클램프로 완화.)
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
    """``asset_id`` 의 단일 다운로드 타깃을 해소한다(노출 게이트 포함, plan D-4).

    Returns:
        registered·비의료면 ``{"asset_id","fs_path","fs_uri","file_size","modality","file_name"}``
        (``file_name`` = ``fs_path`` basename). 행 없음/비registered/의료(FR-014) → ``None`` (API 404).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DOWNLOAD_TARGET_SQL, (asset_id,))
        row = cur.fetchone()

    if row is None:
        return None
    if row["status"] != "registered":
        return None
    if row["domain_label"] == "medical":
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
    """주어진 asset_id 들의 ``fs_path`` 를 일괄 조회해 ``{asset_id: fs_path}`` 로 반환한다."""
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
    """관계 ego-network(seed + 1-hop active 이웃) 묶음 자산을 수집한다(plan D-5).

    절차
        1. ``graph_query``(active, **양방향**)로 이웃을 받아 self-loop 제외·``min_confidence``
           미만 제외·중복 이웃(다중 엣지)은 최고 confidence 로 합친다.
        2. 결정적 정렬 — 이웃을 (confidence desc, asset_id asc)로 줄세운 뒤 ``max_neighbors``
           초과 시 상위 N 으로 절단(허브 자산 메가블롭 방지, 절단 시 ``log`` 경고).
        3. seed 를 맨 앞에 두고 각 자산의 ``fs_path``/``file_name`` 을 채워 반환.

    Returns:
        ``[{"asset_id","fs_path","file_name"}, ...]`` — seed 먼저, 그다음 이웃(결정적 순서).
        이웃 0이면 seed 단독 1건(US3 Acc 3). asset 행이 없어 경로를 모르는 항목은 제외.
    """
    seed_id = str(seed_asset_id)
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
    """zip 엔트리명 충돌 시 ``root_1.ext`` 식 접미사로 유일화한다(결정적)."""
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


# 069 P1-2: 스트리밍 복사 청크(64KiB) — 원본을 통째로 메모리에 올리지 않는다.
_BUNDLE_COPY_CHUNK = 64 * 1024
# zip 결과가 이 크기를 넘으면 메모리 대신 디스크 임시파일로 굴린다(요청당 메모리 상한).
_BUNDLE_SPOOL_MAX = 64 * 1024 * 1024


def build_bundle_zip_stream(targets: list[dict[str, Any]]) -> tempfile.SpooledTemporaryFile:
    """묶음 타깃들의 원본 파일을 zip **스트림(파일류)**로 만든다(069 P1-2 — OOM 차단 코어).

    기존 ``build_bundle_zip``(bytes)이 파일마다 ``fh.read()`` 전체 적재 + 전체 zip 을 BytesIO 로
    들고 있어 대용량 묶음에서 OOM/DoS 소지가 있었다. 원본→zip 엔트리를 ``copyfileobj``(64KiB)로
    흘리고, 결과도 ``SpooledTemporaryFile``(64MiB 초과 시 디스크)로 받아 메모리가 묶음 크기와
    무관해진다. 반환 파일은 위치 0 으로 되감아 주며, 호출자가 소비 후 닫는다(StreamingResponse
    는 응답 종료 시 자동 close).

    부분 zip 보장(Edge Case) · 결정성(헌법 3조)은 기존과 동일 — 누락은 skip+``_manifest.json``,
    엔트리 순서=``targets`` 순서, 타임스탬프 1980-01-01 고정(동일 입력 2회 동일 바이트).
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
        # 리뷰(069 🟡1): 중간 실패(디스크 오류 등) 시 spool 파일 핸들이 누수되지 않게 닫고 재전파
        # (64MiB 초과분은 실제 디스크 임시파일이라 FD 누수 실해가 있다). 성공 경로만 열어서 반환.
        spool.close()
        raise

    spool.seek(0)
    return spool


def build_bundle_zip(targets: list[dict[str, Any]]) -> bytes:
    """묶음 zip 을 bytes 로 돌려주는 하위호환 래퍼(plan D-5 — 코어는 ``build_bundle_zip_stream``).

    069 P1-2 이후 라우트는 스트림 코어를 직접 쓰고, 이 래퍼는 zip **내용 검증용**(테스트·소규모)
    으로 남는다. 파일별 전체 적재는 코어에서 이미 제거됐고, 여기서만 완성 zip 을 한 번에 읽는다.
    """
    with build_bundle_zip_stream(targets) as spool:
        return spool.read()


def _write_zip_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    """타임스탬프 고정 ZipInfo 로 엔트리를 쓴다(결정적 바이트, 헌법 3조)."""
    info = zipfile.ZipInfo(filename=arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)
