"""자산 하나의 상세를 모아 온다 — 메타 · 임베딩 요약 · 관계 이웃(읽기 전용).

**흐름에서의 위치**: 상세 화면 요청이 여기로 온다. 세 곳(자산·임베딩·그래프)에서 읽어
한 응답으로 합치는 것이 이 모듈의 일이다.

설계 판단 셋
    - 임베딩은 **개수만** 센다. 벡터 자체는 응답에 쓸모가 없고 크기만 키운다.
    - 관계는 **이웃 자산 단위로 미리 묶는다**. 같은 이웃과 엣지가 여러 개면 화면이 중복
      카드를 그리게 되는데, 그 병합을 프론트마다 다시 구현하지 않도록 여기서 끝낸다.
    - 관계 조회는 그래프 읽기 통로를 거친다 — 직접 쿼리하면 대칭 엣지의 절반을 놓친다.

**노출 게이트**: 자산이 없거나 아직 등록 완료가 아니면 ``None`` 을 돌려준다(화면은 404).
두 경우를 같게 다뤄 "존재하지만 못 본다"는 정보가 새지 않게 한다.

확장 메타 읽기 규칙
    ``clearance`` 지정 시 ``fetch_access_tiers`` + ``project_ext_meta`` — tier 미달 키 **제거(omit)**.
    권한이 못 보는 키는 **아예 빼고** 내보낸다 — null 이나 '***' 로 바꾸지 않는다(그 자체가
    '항목이 있다'는 정보를 흘리기 때문). DB 원본은 그대로 둔다.
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
# 경로에서 표시용 파일명을 파생한다. 경로는 항상 존재하며
# 검색 색인(opensearch_sync)·review 등 전 표면이 fs_path basename 을 파일명으로 쓰는 관례와 일치한다.
_FETCH_ASSET_SQL = """
SELECT a.asset_id, a.modality, a.domain_label, a.status, a.fs_path,
       m.core_meta, m.ext_meta, m.tags
FROM asset a
LEFT JOIN asset_metadata m ON m.asset_id = a.asset_id
WHERE a.asset_id = %s
LIMIT 1
"""

# 채널별 **개수만** 센다(벡터 자체는 응답에 쓸모가 없다). 정렬을 고정해 순서를 안정시킨다.
_FETCH_EMBEDDING_CHANNELS_SQL = """
SELECT channel, COUNT(*) AS chunk_count
FROM asset_embedding
WHERE asset_id = %s
GROUP BY channel
ORDER BY channel
"""

# 이웃 단위로 묶을 때 각 엣지에서 살려 둘 항목들(어떤 관계였는지 화면이 보여줄 수 있게).
# asset_id/file_name/modality/status 는 이웃 레벨로 승격되므로 엣지에서 제외한다.
_EDGE_DETAIL_KEYS = ("edge_id", "kind_code", "confidence", "direction", "is_symmetric", "topic", "reason")


def _conf_sort_key(confidence: Any) -> float:
    """confidence 를 오름차순 정렬키로 인코딩 — ``desc`` + ``NULLS LAST``.

    수치는 부호를 뒤집어(큰 값일수록 앞) 쓰고, 값이 없으면 무한대로 두어 **항상 맨 뒤**로 민다.

    Args:
        confidence: 신뢰도. ``None``·비수치도 받는다.

    Returns:
        오름차순 정렬에 쓸 키.
    graph_query 의 ``ORDER BY confidence DESC NULLS LAST`` 와 동형(결정성·헌법 3조).
    """
    return -confidence if isinstance(confidence, (int, float)) else float("inf")


def _merge_relations_by_asset(neighbor_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """엣지 목록을 **이웃 자산 단위로 묶는다**.

    그래프 조회는 **엣지 하나가 한 행**이라, 같은 이웃과 관계가 둘이면 그 이웃이 두 번 나온다.
    화면은 이웃 하나를 카드 하나로 그리므로 여기서 묶는다. 묶는 일을 화면에 맡기면 화면마다
    따로 구현돼 서로 달라진다.

    묶는 규칙 — 모두 순서가 고정된다(같은 입력이면 같은 출력):
      - 자산 id 로 묶고, 파일명·모달리티는 엣지에 실려 온 값을 그대로 올려 쓴다(재조회 없음).
      - 관계 종류는 중복을 없애 이름순으로.
      - 대표 신뢰도는 엣지 중 최댓값. 전부 값이 없으면 ``None``.
      - 엣지 상세는 원본 그대로 남기고 신뢰도 높은 순으로.

    Args:
        neighbor_edges: 그래프 조회가 준 **엣지 단위** 목록. 같은 이웃이 여러 번 나올 수 있다.

    Returns:
        이웃 자산 단위 목록. 각 항목은 ``asset_id``·``file_name``·``modality``·``kind_codes``·
        ``max_confidence``·``edges``(원본 상세)를 갖는다. 입력이 비면 빈 목록.
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

    # 정렬은 **마지막에 한 번만** 한다 — 위 반복은 입력이 온 순서대로 쌓으므로, 그대로 두면
    # 조회 순서가 조금만 달라져도 화면 순서가 바뀐다.
    merged: list[dict[str, Any]] = []
    for g in groups.values():
        edges = g["edges"]
        # 신뢰도 같으면 엣지 id 로 갈라 순서를 못 박는다(동점 순서가 흔들리지 않게).
        edges.sort(key=lambda ed: (_conf_sort_key(ed["confidence"]), str(ed["edge_id"])))
        # 값이 없는 엣지를 섞으면 max() 가 터진다 — 수치만 골라 낸다.
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
    """자산 상세를 조립한다 — 메타·임베딩 요약·관계 이웃을 한 번에.

    조회 전용(쓰기 없음). 세 곳에서 읽어 하나로 합친다.

    Args:
        asset_id: 대상 자산.
        clearance: 요청자 권한 등급. 주면 그 등급이 못 보는 메타 항목을 **빼고** 담는다
            (null 이나 마스킹 문자열로 바꾸지 않는다 — 키의 존재 자체가 정보이기 때문).
            ``None`` 이면 원본 그대로다(내부 호출·테스트 경로).

    Returns:
        상세 dict. **자산이 없거나 아직 등록 완료가 아니면 ``None``** — 둘을 같게 다뤄
        "존재하지만 못 본다"는 정보가 새지 않게 한다(호출부는 404 로 바꾼다).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FETCH_ASSET_SQL, (asset_id,))
        row = cur.fetchone()

    # 노출 여부 판정
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

    # 엣지 단위 목록을 이웃 자산 단위로 미리 묶는다(화면이 중복 카드를 그리지 않게).
    relations = _merge_relations_by_asset(fetch_active_relations_for_asset(conn, asset_id=asset_id))

    ext_meta = row["ext_meta"]
    if clearance is not None:
        # 권한이 주어지면 그에 맞게 가리고, 없으면(내부 호출·테스트) 원본 그대로 둔다.
        domain = str(row["domain_label"])
        tiers = fetch_access_tiers(conn, domain)  # 도메인별 항목 등급표
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
        # 표시용 파일명을 함께 내려 준다 — 검색 결과와 **같은 함수**를 써서 표기가 어긋나지 않게.
        "file_name": display_name(str(row["fs_path"] or "")),
        "core_meta": row["core_meta"],
        "ext_meta": ext_meta,
        "tags": row["tags"],
        "embedding_channels": embedding_channels,
        "relations": relations,
    }
