"""포탈 자산 상세 조회 — 단일 자산의 메타·임베딩 채널 요약·관계 이웃 (spec 010 D-3 · 042).

조회 구성(읽기 전용)
    1. ``asset`` + ``LEFT JOIN asset_metadata`` 1행 — modality/도메인/상태 + core/ext_meta/tags.
    2. ``asset_embedding`` 채널별 청크 **개수만** 집계 — 원시 벡터(1536D) 미반환(FR-005).
    3. 관계 이웃 — ``graph_query`` read seam(양방향·active, FR-006)을 **이웃 자산 단위로 사전 병합**
       (``_merge_relations_by_asset``·FR-201): 같은 이웃과의 다중 엣지를 1행으로 묶어 kind_codes·
       max_confidence·edges 로 정규화(결정적 정렬). 프론트 mergeRelationsByAsset 재구현 제거.

노출 게이트(FR-014)
    행 없음 / ``status != 'registered'`` → ``None`` (API 404). (2026-07-23: 도메인 제외 전면 제거.)

ext_meta read 집행(042 · 040 tier · 041 레지스트리)
    ``clearance`` 지정 시 ``fetch_access_tiers`` + ``project_ext_meta`` — tier 미달 키 **제거(omit)**.
    null·마스킹 문자열 치환 없음(plan D2). DB 원본은 전량 유지(ingest 039).
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from service.portal.search_group import display_name
from src.registry.access_tier import project_ext_meta
from src.registry.ext_meta_field_registry import fetch_access_tiers
from src.relations.graph_query import fetch_active_relations_for_asset

# asset + metadata 1행. LEFT JOIN — 메타 없어도 자산 행 유지(core/ext NULL 가능).
# fs_path(057 FR-101) — 최상위 file_name basename 파생용. fs_path 는 NOT NULL 이라 항상 존재하며
# 검색 색인(opensearch_sync)·review 등 전 표면이 fs_path basename 을 파일명으로 쓰는 관례와 일치한다.
_FETCH_ASSET_SQL = """
SELECT a.asset_id, a.modality, a.domain_label, a.status, a.fs_path,
       m.core_meta, m.ext_meta, m.tags
FROM asset a
LEFT JOIN asset_metadata m ON m.asset_id = a.asset_id
WHERE a.asset_id = %s
LIMIT 1
"""

# 임베딩 채널별 청크 개수만(FR-005). ORDER BY channel — 결정적.
_FETCH_EMBEDDING_CHANNELS_SQL = """
SELECT channel, COUNT(*) AS chunk_count
FROM asset_embedding
WHERE asset_id = %s
GROUP BY channel
ORDER BY channel
"""

# 057 FR-201: relations 응답을 이웃 자산 단위로 사전 병합할 때 각 엣지에서 보존하는 상세 키.
# asset_id/file_name/modality/status 는 이웃 레벨로 승격되므로 엣지에서 제외한다.
_EDGE_DETAIL_KEYS = ("edge_id", "kind_code", "confidence", "direction", "is_symmetric", "topic", "reason")


def _conf_sort_key(confidence: Any) -> float:
    """confidence 를 오름차순 정렬키로 인코딩 — ``desc`` + ``NULLS LAST``.

    수치는 ``-confidence`` 로(큰 값일수록 작은 키 → 앞), None(및 비수치)은 ``+inf`` 로 항상 맨 뒤.
    graph_query 의 ``ORDER BY confidence DESC NULLS LAST`` 와 동형(결정성·헌법 3조).
    """
    return -confidence if isinstance(confidence, (int, float)) else float("inf")


def _merge_relations_by_asset(neighbor_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """관계 이웃-엣지 목록을 이웃 자산(``asset_id``) 단위로 사전 병합(FR-201).

    ``graph_query.fetch_active_relations_for_asset`` 는 엣지 단위 목록(같은 이웃과 복수 엣지 가능)을
    주는데, 프론트가 이웃 카드 하나로 렌더하려면 asset_id 로 묶어야 한다. 그 병합·결정적 정렬을
    프론트(``mergeRelationsByAsset`` 2곳 중복 재구현)에서 걷어내 서버 단일 진실로 이관한다.
    엣지 단위 seam 자체는 불변(다른 소비자 광범위·하위호환) — 여기서 그 출력만 그룹핑한다.

    병합 규칙(전부 결정적·헌법 3조):
      - ``asset_id`` 로 그룹핑. ``file_name``·``modality`` 는 G1 이 엣지에 내려둔 값을 이웃 레벨로
        승격(그룹 내 동일·재조회 0). 삽입 순서에 의존하지 않도록 마지막에 정렬한다.
      - ``kind_codes`` = 이웃 엣지들의 ``kind_code`` distinct(오름차순 정렬).
      - ``max_confidence`` = 엣지 confidence 최대. 모든 엣지가 None 이면 None.
      - ``edges`` = 원본 엣지 상세(``_EDGE_DETAIL_KEYS``) 보존. confidence desc → edge_id asc 정렬.
      - 이웃 목록 정렬 = max_confidence desc(None 최후) → asset_id asc.
    """
    groups: dict[str, dict[str, Any]] = {}
    for e in neighbor_edges:
        aid = str(e["asset_id"])
        g = groups.get(aid)
        if g is None:
            g = {
                "asset_id": aid,
                "file_name": e.get("file_name"),
                "modality": e.get("modality"),
                "kinds": set(),
                "edges": [],
            }
            groups[aid] = g
        g["kinds"].add(e["kind_code"])
        g["edges"].append({k: e.get(k) for k in _EDGE_DETAIL_KEYS})

    merged: list[dict[str, Any]] = []
    for g in groups.values():
        edges = g["edges"]
        edges.sort(key=lambda ed: (_conf_sort_key(ed["confidence"]), str(ed["edge_id"])))
        numeric = [ed["confidence"] for ed in edges if isinstance(ed["confidence"], (int, float))]
        merged.append(
            {
                "asset_id": g["asset_id"],
                "file_name": g["file_name"],
                "modality": g["modality"],
                "kind_codes": sorted(g["kinds"]),
                "max_confidence": max(numeric) if numeric else None,
                "edges": edges,
            }
        )
    merged.sort(key=lambda n: (_conf_sort_key(n["max_confidence"]), n["asset_id"]))
    return merged


def fetch_asset_detail(
    conn: Connection[Any],
    *,
    asset_id: str,
    clearance: str | None = None,
) -> dict[str, Any] | None:
    """자산 상세 조립. ``clearance`` 지정 시 ext_meta tier 미달 키 omit(042)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FETCH_ASSET_SQL, (asset_id,))
        row = cur.fetchone()

    # 노출 게이트(FR-014)
    if row is None:
        return None
    if row["status"] != "registered":
        return None

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FETCH_EMBEDDING_CHANNELS_SQL, (asset_id,))
        channel_rows = cur.fetchall()
    embedding_channels = [
        {"channel": r["channel"], "chunk_count": int(r["chunk_count"])} for r in channel_rows
    ]

    # FR-201(057): 엣지 단위 이웃 목록을 이웃 자산 단위로 사전 병합(결정적). seam 호출은 불변.
    relations = _merge_relations_by_asset(fetch_active_relations_for_asset(conn, asset_id=asset_id))

    ext_meta = row["ext_meta"]
    if clearance is not None:
        # 포탈 API(042) — principal.clearance 로 read projection. None 이면 DB 원본 그대로(내부·테스트).
        domain = str(row["domain_label"])
        tiers = fetch_access_tiers(conn, domain)  # 040/041 레지스트리
        ext_meta = project_ext_meta(
            ext_meta if isinstance(ext_meta, dict) else {},
            tiers,
            domain=domain,
            clearance=clearance,
        )

    return {
        "asset_id": str(row["asset_id"]),
        "modality": row["modality"],
        "domain_label": row["domain_label"],
        "status": row["status"],
        # FR-101(057): 표시용 파일명 하향(하위호환 필드 추가) — search_group.display_name 단일 출처 재사용.
        "file_name": display_name(str(row["fs_path"] or "")),
        "core_meta": row["core_meta"],
        "ext_meta": ext_meta,
        "tags": row["tags"],
        "embedding_channels": embedding_channels,
        "relations": relations,
    }
