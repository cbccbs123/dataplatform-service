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
from src.config.settings import get_current_settings
from src.registry.access_tier import project_ext_meta
from src.registry.ext_meta_field_registry import fetch_access_tiers
from src.relations.graph_query import fetch_relations_for_asset

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
#
# 081 항목6 에서 둘을 더했다 — 넣지 않으면 코어가 계산한 것이 **여기서 버려진다**:
#   tier              노출 등급("strong"=연관 자료 / "weak"=참고 자료). 화면이 두 칸을 가르는 근거.
#   folded_kind_codes 동시보유 접기에서 접힌 종류(대개 빈 리스트). 접은 사실을 **관측 가능**하게
#                     둔다 — 없으면 "DB 3,357 vs 화면 3,177" 의 차이를 설명할 수단이 사라진다.
_EDGE_DETAIL_KEYS = (
    "edge_id", "kind_code", "confidence", "direction", "is_symmetric", "topic", "reason",
    "tier", "folded_kind_codes",
)

# 이웃 목록 정렬 우선순위 — **강칸이 먼저다**(코어 `graph_query._TIER_RANK` 와 같은 값).
# ⚠️ 이 표가 없으면 아래 정렬이 신뢰도만 보고, 그러면 **고신뢰 약칸이 저신뢰 강칸을 밀어낸다** —
# 사람이 확인해 준 관계가 아래로 내려간다. 코어가 등급 순으로 정렬해 넘겨도 여기서 다시
# 정렬하므로, 등급을 여기 정렬키에 넣지 않으면 코어의 정렬이 **무효화**된다(081 항목6 에서
# 발견한 결함 — 그때까지 active 가 0건이라 증상이 드러나지 않았다).
_TIER_RANK = {"strong": 0, "weak": 1}


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
        ``max_confidence``·``tier``·``edges``(원본 상세)를 갖는다. 입력이 비면 빈 목록.
        정렬은 **등급 → 신뢰도 → asset_id** 다(등급이 신뢰도보다 앞선다 — 아래 참조).
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
        # 이웃의 등급 = 그 이웃에 붙은 엣지 중 **가장 높은 것**. 코어가 이웃당 하나로 접어
        # 보내므로 보통 엣지는 1건이지만, 접기가 꺼지거나 계약이 바뀌어도 강칸을 잃지 않게
        # 최댓값을 쓴다(약칸 하나 때문에 확인된 관계가 아래로 밀리면 안 된다).
        tier = min((str(ed.get("tier") or "") for ed in edges),
                   key=lambda t: _TIER_RANK.get(t, len(_TIER_RANK)), default="")
        merged.append(
            {
                "asset_id": g["asset_id"],
                "file_name": g["file_name"],
                "modality": g["modality"],
                "kind_codes": sorted(g["kinds"]),
                "max_confidence": max(numeric) if numeric else None,
                # 081 항목6 — 이웃 레벨 등급. 화면이 "연관 자료"/"참고 자료" 두 칸을 가르는 값.
                "tier": tier,
                "edges": edges,
            }
        )
    # ⚠️ **등급이 신뢰도보다 앞선다.** 신뢰도만으로 정렬하면 고신뢰 약칸(참고 자료)이 저신뢰
    # 강칸(연관 자료)을 밀어내 **사람이 확인해 준 관계가 아래로 내려간다**. 코어도 같은 순서로
    # 넘기지만 여기서 다시 정렬하므로 등급을 키에 넣지 않으면 그 정렬이 무효화된다.
    merged.sort(key=lambda n: (_TIER_RANK.get(str(n["tier"]), len(_TIER_RANK)),
                               _conf_sort_key(n["max_confidence"]), n["asset_id"]))
    return merged


def fetch_asset_detail(
    conn: Connection[Any],
    *,
    asset_id: str,
    clearance: str | None = None,
    min_conf_similarity: float | None = None,
) -> dict[str, Any] | None:
    """자산 상세를 조립한다 — 메타·임베딩 요약·관계 이웃을 한 번에.

    조회 전용(쓰기 없음). 세 곳에서 읽어 하나로 합친다.

    Args:
        asset_id: 대상 자산.
        clearance: 요청자 권한 등급. 주면 그 등급이 못 보는 메타 항목을 **빼고** 담는다
            (null 이나 마스킹 문자열로 바꾸지 않는다 — 키의 존재 자체가 정보이기 때문).
            ``None`` 이면 원본 그대로다(내부 호출·테스트 경로).
        min_conf_similarity: 관계 노출 하한. ``None``(기본)이면 **설정에서 읽는다**
            (`RELATION_PERSIST_MIN_CONF_SIMILARITY`). 값을 주면 설정을 읽지 않는다 —
            ⚠️ 이 인자가 있는 이유는 **설정 초기화 없이 도는 순수 단위 테스트** 때문이다.
            운영 경로는 넘기지 않는 쪽이며, 그래야 영속화 게이트와 값이 갈리지 않는다.

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
    #
    # 081 항목6 — **확인 전(`proposed`)도 함께 읽는다.** 종전에는 `active` 만 읽었는데,
    # 자동승인을 폐지한 뒤(2026-07-31) `active` 를 만드는 길이 사람 승인뿐이라 **19일간 0건**
    # 이었고 그래서 **모든 자산의 관계 영역이 비어 있었다**(엣지는 3,357건 있는데 화면은 0건).
    # 승인이 노출의 관문으로 남아 있는 한 화면은 영구히 빈다 — 그래서 관문을 등급으로 바꾼다.
    # 확인 여부는 사라지지 않고 `tier` 로 구분된다(strong=연관 자료 / weak=참고 자료).
    #
    # ⚠️ 노출 하한은 **영속화 게이트와 같은 값**을 써야 한다. 다르면 "행은 만들어지는데 화면엔
    # 없는" 유령 구간이 생기고 그건 조용히 생긴다(코어 `approval_policy` 가 이 일치를 테스트로
    # 봉인한다). 그래서 상수를 새로 두지 않고 **설정에서 읽는다**.
    min_conf = (min_conf_similarity if min_conf_similarity is not None
                else get_current_settings().relations.persist_min_conf_similarity)
    relations = _merge_relations_by_asset(
        fetch_relations_for_asset(
            conn, asset_id=asset_id, include_weak=True, min_conf_similarity=min_conf))

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
