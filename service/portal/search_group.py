"""검색 결과를 **모달리티별 독립 순위**로 묶는다 — 순수 함수(DB·IO 없음).

**흐름에서의 위치**: 검색이 돌려준 모달리티별 버킷을 화면이 그릴 모양으로 정리한다.

**모달리티를 하나의 순위로 합치지 않는다**
    버킷마다 점수를 내는 방식이 달라 숫자의 뜻이 서로 다르다 — 같은 0.8 이 텍스트와 영상에서
    같은 정도의 적합함을 뜻하지 않는다. 한 줄로 세우면 구조적으로 점수가 큰 모달리티가 상단을
    독식하고 나머지는 통째로 밀려난다. 그래서 **모달리티 안에서만** 비교한다. 화면도 어차피
    섹션별로 나눠 보여 준다.

**동점 순서를 못 박는다** — 점수가 같을 때 자산 id 로 갈라, 같은 질의가 매번 같은 순서를 낸다.

⚠️ **표준 라이브러리만 import 한다.** 검색 쪽 모듈을 들여오면 임베딩 모델까지 딸려 올라와,
이 모듈을 쓰는 가벼운 테스트가 무거워진다. 점수 정화 같은 작은 일도 여기서 다시 만든다.
"""

from __future__ import annotations

import math
import re
from typing import Any

# 파일명 처리는 코어 함수를 그대로 쓴다(같은 규칙이 두 곳에 생기지 않게) —
# 끌지 않으므로 본 모듈의 "표준 라이브러리만 import" 순수 계약(torch 등 미로드)이 유지된다.
from src.config.filename_util import basename_of

# 저장할 때 붙인 id 접두(``{asset_id}__``)를 화면용 파일명에서 벗겨 내는 패턴.
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
    """행의 점수를 **유한한 실수**로 읽는다.

    같은 일을 하는 함수가 검색 쪽에도 있지만 가져다 쓰지 않는다 — 그 모듈을 import 하면
    임베딩 모델까지 딸려 올라와, 표준 라이브러리만 쓰는 이 모듈의 가벼움이 깨진다.

    Args:
        row: 결과 행. 점수 키가 없거나 값이 이상해도 예외를 올리지 않는다.

    Returns:
        유한 실수. 읽을 수 없거나 NaN·무한대면 0.0(정렬이 실행마다 달라지는 것을 막는다).
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
    id 접두 제거를 합쳐서 처리한다. 경로에서 파일명만 뽑는 일은 코어 함수가 맡고,
    프리픽스 제거는 표시 전용 책임이라 여기서만 얹는다(색인·샘플 경로는 프리픽스를 벗기지 않음).

    Args:
        uri: 자산 경로 또는 URI.

    Returns:
        표시용 파일명. 입력이 비면 빈 문자열.

    **공개 심볼**: 상세 응답(``asset_detail``)·테스트가 모듈 경계 너머로 재사용하는 표시명 단일
    여러 모듈이 함께 쓰므로 밑줄 없는 공개 이름으로 둔다(비공개 이름을 남이 import 하지 않게).
    """
    return _ASSET_ID_PREFIX.sub("", basename_of(uri))


def _sort_key(item: dict[str, Any]) -> tuple[float, str]:
    """버킷 안 정렬 키 — 유사도 내림차순, 동점은 자산 id 순.

    유사도를 반올림해 비교하는 이유: 부동소수 끝자리 차이로 순서가 뒤집히면 같은 질의가
    실행마다 다른 순서를 낸다.

    Args:
        item: 결과 행.

    Returns:
        정렬 키 튜플.
    """
    return (-round(_row_similarity(item), 6), str(item.get("asset_id", "")))


def _shape(row: dict[str, Any], modality: str) -> dict[str, Any]:
    """원시 검색 행 → 포탈 응답 항목.

    ``domain_label``: 권한별 필드 가리기를 계산할 때 쓴다.

    Args:
        row: 검색 엔진이 준 원시 행.
        modality: 이 행이 속한 버킷 이름(행에 함께 담는다).

    Returns:
        응답 항목 dict. 도메인 라벨이 없으면 기본값으로 채운다(뒤 단계가 None 을 만나지 않게).
    """
    return {
        "asset_id": str(row.get("id", "")),
        "modality": modality,
        "similarity": _row_similarity(row),
        "summary": row.get("summary", "") or "",
        "file_name": display_name(str(row.get("file_uri", ""))),
        "domain_label": row.get("domain_label") or "general",
        # 주제 패싯·결과 좁히기에 쓸 값을 그대로 통과시킨다 — 화면이 이미 받은 결과로
        # 이 topics 로 클라 필터(재검색 없이) → 패싯 수와 표시 수 일치·컷오프 무관.
        "topics": [str(t) for t in (row.get("topics") or [])],
        "subtopics": [str(t) for t in (row.get("subtopics") or [])],
        # 부모·자식을 따로 내리면 화면이 둘을 곱해 있지도 않은 조합을 그린다 — 짝을 그대로 내린다.
        # 색인에 짝이 없는 옛 문서는 빈 목록으로 와, 화면이 부모 목록만으로 그리게 된다.
        "topic_pairs": [str(t) for t in (row.get("topic_pairs") or [])],
    }


def group_ranked(
    search_result: dict[str, Any],
    *,
    limit_per_modality: int,
    exclude_domains: frozenset[str] = frozenset(),
) -> dict[str, list[dict[str, Any]]]:
    """모달리티 버킷 dict 를 모달리티별 독립 랭킹 ``{modality: [rows]}`` 로 묶는다.

    모달리티끼리 **점수를 섞지 않는다** — 척도가 달라 한 줄로 세우면 특정 모달리티가 통째로
    밀려난다. 그래서 버킷마다 따로 순위를 매긴다.

    Args:
        search_result: 검색이 돌려준 원시 결과.
        limit_per_modality: 버킷당 담을 최대 건수. **1 미만이면 예외** — 0을 허용하면
            빈 결과가 정상인지 설정 실수인지 구분되지 않는다.
        exclude_domains: 제외할 도메인. 기본은 비어 있다(전 도메인 노출).

    Returns:
        ``{modality: [rows]}``. **입력에 있던 버킷만** 키로 나타난다(빈 입력이면 빈 dict).

    Raises:
        ValueError: ``limit_per_modality`` 가 1 미만일 때.
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
                continue  # 배제 목록에 든 도메인
            shaped.append(_shape(row, modality))
        shaped.sort(key=_sort_key)
        grouped[modality] = shaped[:limit_per_modality]
    return grouped
