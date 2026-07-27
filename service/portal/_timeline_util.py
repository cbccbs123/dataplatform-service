"""시계열 결과를 화면이 쓰기 좋은 형태로 뒤집는 공용 헬퍼(순수·읽기 전용).

계보·접근이력·자산 타임라인이 **모두 같은 응답 모양**을 쓴다. 접는 로직을 각자 두면 한쪽만
고쳐져 화면이 서로 다른 모양을 받게 되므로 여기 한 곳에 둔다.
"""
from __future__ import annotations

from typing import Any

# 시간 버킷 단위 허용 목록 — **여기 하나뿐**이다(라우트·서비스가 각자 두면 서로 어긋난다).
# ⚠️ 이 값은 SQL 문자열에 그대로 박히므로, 목록을 통과한 값만 써야 한다.
TIMELINE_INTERVALS: tuple[str, ...] = ("day", "hour", "month")


def pivot_series(grouped_rows: list[tuple]) -> list[dict]:
    """(key, bucket, count) 행(key ASC·bucket ASC 정렬 가정)을 멀티시리즈로 피벗(순서 보존·결정적).

    DB 가 이미 정렬해 주므로 넣은 순서가 곧 시리즈 순서다 — 여기서 다시 정렬하지 않는다.

    Args:
        grouped_rows: ``(시리즈 키, 버킷, 개수)`` 행들. **정렬된 상태로 들어와야** 한다.

    Returns:
        ``[{key, buckets}]`` 시리즈 목록.
    """
    series_map: dict[Any, list] = {}
    order: list = []
    for key, bkt, count in grouped_rows:
        if key not in series_map:
            series_map[key] = []
            order.append(key)
        series_map[key].append(
            {"bucket": bkt.isoformat() if bkt is not None else None, "count": int(count)})
    return [{"key": k, "buckets": series_map[k]} for k in order]
