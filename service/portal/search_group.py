"""검색 결과를 **모달리티별 독립 랭킹**으로 묶는다 — 완전 순수 함수(spec 010 grouped).

``search_service.search_hybrid`` 의 모달리티 버킷 결과를 전 모달리티 합치지 않고(평탄화 금지),
각 모달리티 섹션 안에서만 결정적으로 랭킹해 ``{modality: [rows]}`` 로 돌려준다.

왜 평탄화하지 않나(원인 ③ 회피)
    버킷마다 점수 산식이 달라(텍스트 하이브리드 alpha vs 영상 2단계+bm25) **모달리티 간 점수
    척도가 비교 불가**다. 이를 단일 ``similarity`` 로 합쳐 정렬하면 구조적으로 점수가 높은 영상이
    상단을 독식한다. 그래서 cross-modal 비교를 아예 하지 않고, **모달리티 내부에서만** 비교한다 —
    포탈은 어차피 섹션(text/image/video/audio)별로 데이터를 분류해 제공하면 되기 때문(설계 승인).

설계 요지
    - 정렬 키(결정성·헌법 3조): 버킷 내 ``(-round(similarity, 6), asset_id)``. similarity 정화는
      아래 ``_row_similarity`` 로 None/NaN/inf → 0.0 처리(순수·표준 라이브러리만).
    - 각 버킷은 ``limit_per_modality`` 까지만(섹션별 top-N, 페이징 없음 — 설계 승인).
    - ``exclude_domains`` 에 드는 ``domain_label`` 행은 해당 버킷에서 제외(FR-014, 의료 배제).
    - ``domain_label`` 을 응답 항목에 포함(042) — ``portal_api`` tier projection 에 사용.

DB·네트워크·파일 IO 없음. 표준 라이브러리만 import(완전 순수).
"""

from __future__ import annotations

import math
import re
from typing import Any

# 파일명 코어(basename_of)는 단일 출처(069 D3) — src/config 는 empty __init__ 라 heavy 의존을
# 끌지 않으므로 본 모듈의 "표준 라이브러리만 import" 순수 계약(torch 등 미로드)이 유지된다.
from src.config.filename_util import basename_of

# 아카이브 asset_id 프리픽스(``{asset_id}__``) 역패턴 — 표시용 파일명서 벗긴다(065 T605).
# 정본은 파이프라인 레포 ``processing/ingest/archiver.py::_ASSET_ID_PREFIX`` 다. 레포 분리로 백엔드는
# 크로스레포 import 를 하지 않으므로 같은 UUIDv7 패턴을 작은 사본으로 둔다(포맷 변경 시 양쪽 동기).
_ASSET_ID_PREFIX = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}__"
)

# 결과 버킷 키 → 모달리티 라벨. search_service._MODALITY_BUCKETS 의 역매핑과 같은 표를 포탈
# 계층에 작은 사본으로 둔다(검색 서비스에 묶이지 않도록). 미지정 버킷은 키 그대로 노출.
_BUCKET_TO_MODALITY = {
    "text_documents": "text",
    "audio": "audio",
    "image": "image",
    "video": "video",
}


def _row_similarity(row: dict[str, Any]) -> float:
    """행의 ``similarity`` 를 유한 실수로 읽는다(None/NaN/inf/비수치 → 0.0).

    본 모듈은 "표준 라이브러리만 import" 순수 계약이라(검색 서비스를 import 하면 opensearch_search→
    임베더(torch) 체인을 끌어옴) similarity 정화를 여기 작은 순수 함수로 둔다.
    """
    value = row.get("similarity")
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def display_name(uri: str) -> str:
    """file_uri(전체 경로/URI)에서 **표시용** 파일명을 뽑는다(결정적·순수).

    공통 코어 ``basename_of``(쿼리/프래그먼트 제거·백슬래시 정규화) 위에 아카이브 asset_id
    프리픽스(``{asset_id}__``) 제거를 합성한다(065 T605). basename 추출은 단일 출처(069 D3)이고,
    프리픽스 제거는 표시 전용 책임이라 여기서만 얹는다(색인·샘플 경로는 프리픽스를 벗기지 않음).

    **공개 심볼**: 상세 응답(``asset_detail``)·테스트가 모듈 경계 너머로 재사용하는 표시명 단일
    출처이므로 언더스코어 비공개가 아니라 공개 이름으로 노출한다(069 D3 후속·P2-27 private import 해소).
    """
    return _ASSET_ID_PREFIX.sub("", basename_of(uri))


def _sort_key(item: dict[str, Any]) -> tuple[float, str]:
    """버킷 내 결정적 정렬 키: 유사도 내림차순(round 6), 동점은 asset_id 오름차순."""
    return (-round(_row_similarity(item), 6), str(item.get("asset_id", "")))


def _shape(row: dict[str, Any], modality: str) -> dict[str, Any]:
    """원시 검색 행 → 포탈 응답 항목.

    ``domain_label``(042): ``fetch_access_tiers``·``domain_floor`` 합성용.
    OS hit 에 없으면 ``general`` 폴백.
    """
    return {
        "asset_id": str(row.get("id", "")),
        "modality": modality,
        "similarity": _row_similarity(row),
        "summary": row.get("summary", "") or "",
        "file_name": display_name(str(row.get("file_uri", ""))),
        "domain_label": row.get("domain_label") or "general",
        # 057-후속: 주제 패싯·결과-좁히기용(os_hit_to_row 색인 topics 통과). 프론트가 로드된 결과를
        # 이 topics 로 클라 필터(재검색 없이) → 패싯 수와 표시 수 일치·컷오프 무관.
        "topics": [str(t) for t in (row.get("topics") or [])],
        "subtopics": [str(t) for t in (row.get("subtopics") or [])],
        # 059 FR-104: 부모>자식 짝(topic_pairs)을 응답 행에 통과시켜 프론트가 topic→subtopic 트리를
        # 교차곱 오배치 없이 그리게 한다(하위호환 필드·미존재 시 [] 폴백·os_hit_to_row 색인 짝 통과).
        "topic_pairs": [str(t) for t in (row.get("topic_pairs") or [])],
    }


def group_ranked(
    search_result: dict[str, Any],
    *,
    limit_per_modality: int,
    exclude_domains: frozenset[str] = frozenset(),
) -> dict[str, list[dict[str, Any]]]:
    """모달리티 버킷 dict 를 모달리티별 독립 랭킹 ``{modality: [rows]}`` 로 묶는다.

    각 항목: ``asset_id``·``modality``·``similarity``·``summary``·``file_name``·``domain_label``.
    ``(-round(similarity,6), asset_id)`` 로 정렬하고(cross-modal 병합 없음), 의료 배제 후 상위
    ``limit_per_modality`` 건만 담는다. 입력에 존재하는 버킷만 결과 키로 등장한다(빈 입력 → ``{}``).
    """
    if limit_per_modality < 1:
        raise ValueError("limit_per_modality 는 1 이상")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for bucket, rows in (search_result.get("results") or {}).items():
        modality = _BUCKET_TO_MODALITY.get(bucket, bucket)
        shaped: list[dict[str, Any]] = []
        for row in rows or []:
            label = row.get("domain_label")
            if label is not None and label in exclude_domains:
                continue  # 의료 등 배제 도메인(FR-014)
            shaped.append(_shape(row, modality))
        shaped.sort(key=_sort_key)
        grouped[modality] = shaped[:limit_per_modality]
    return grouped
