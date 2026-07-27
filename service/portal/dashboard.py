"""운영 대시보드 집계 — 접근·계보·자산 세 영역을 한 트랜잭션에서 조합한다.

**흐름에서의 위치**: 대시보드 화면 하나가 필요한 것을 한 번에 만들어 준다. 화면이 영역·기간별로
따로 부르면 왕복이 쌓이고, 조각마다 다른 순간을 보게 돼 수치가 서로 어긋난다.

설계 판단
    - **새로 계산하지 않는다** — 이미 검증된 조회 함수들을 그대로 부르고 묶기만 한다. 정렬·
      결정성은 그 함수들이 보장하므로 여기서 규칙을 또 두지 않는다.
    - **기준 시각은 호출자가 준다**. "오늘"·"최근 N개월"은 벽시계에 매인 값이라, 안에서
      현재 시각을 읽으면 테스트가 시각에 따라 흔들린다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection

from service.portal.access_log import access_log_stats, access_log_timeline
from service.portal.asset_stats import asset_stats, asset_timeline
from service.portal.lineage_query import lineage_stats, lineage_timeline

_DEFAULT_MONTHS = 6


def _month_floor_back(day_start: datetime, months: int) -> datetime:
    """``day_start``(자정) 기준 (months-1)개월 전 '그 달 1일'을 반환(월 경계 정렬 N개월 창).

    예) 7월 1일에서 6개월을 되짚으면 2월 1일(2~7월 = 6개월). 월 경계에 맞춰야 화면의
    월 단위 묶음과 어긋나지 않는다.

    Args:
        day_start: 기준 날짜(자정).
        months: 거슬러 올라갈 개월 수. **기준 달을 포함해서 센다**(6이면 5개월 전 1일).

    Returns:
        그 달 1일 자정.
    """
    first = day_start.replace(day=1)
    year, month = first.year, first.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return first.replace(year=year, month=month)


def build_dashboard_summary(
    conn: Connection[Any], *, now: datetime, months: int = _DEFAULT_MONTHS,
    monthly_interval: str = "day",
) -> dict[str, Any]:
    """운영 대시보드 3도메인 집계를 한 트랜잭션에서 조합해 반환한다.

    각 도메인 슬라이스:
        - ``kpi_alltime``: 전체 기간 stats(도넛·KPI 총계)
        - ``kpi_today``: 오늘([자정, 익일 자정)) stats
        - ``monthly_timeline``: 최근 ``months``개월 멀티시리즈(group_by·``monthly_interval`` 버킷)
        - ``hourly_timeline``: 오늘 **시간별** 멀티시리즈(group_by)

    조회 전용·도메인 제외 없음(각 함수)·LLM 0·마이그레이션 0. 윈도우는 ``now`` 로 계산(주입·결정적 테스트).
    반환 ``{access:{...}, lineage:{...}, asset:{...}, meta:{...}}``.

    ``monthly_interval`` — 월별 슬라이스의 버킷 단위. 기본은 일별이며(기존
    동작 완전 불변). ``"month"`` 면 월 버킷으로 내려 프론트의 일→월 롤업(``rollupTimelineSeriesToMonth``)을
    제거한다(각 timeline 함수의 TIMELINE_INTERVALS 화이트리스트·API 계층이 day|month 로 선검증). 시간별
    슬라이스는 항상 시간 단위다.

    Args:
        now: 기준 시각. **인자로 받는 이유는 테스트가 시각을 고정할 수 있어야** 하기 때문이다
            (내부에서 현재 시각을 읽으면 같은 입력이 매번 다른 결과를 낸다).
        months: 월별 슬라이스가 거슬러 올라갈 개월 수.
        monthly_interval: 월별 슬라이스의 버킷 단위(일별·월별). 시간별 슬라이스와는 무관하다.

    Returns:
        ``{access, lineage, asset, meta}`` — 세 영역 집계와 계산에 쓴 기준값.
    """
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    month_start = _month_floor_back(today_start, months)

    def _domain_slice(stats_fn: Any, timeline_fn: Any, group_by: str) -> dict[str, Any]:
        """한 영역의 지표 묶음을 만든다 — 조회 함수만 갈아끼워 세 영역에 재사용한다.

        Args:
            stats_fn: 그 영역의 집계 함수.
            timeline_fn: 그 영역의 추이 함수.
            group_by: 추이를 가를 기준.

        Returns:
            전체 지표·오늘 지표·월별 추이를 담은 dict.
        """
        return {
            "kpi_alltime": stats_fn(conn),
            "kpi_today": stats_fn(conn, since=today_start, until=today_end),
            "monthly_timeline": timeline_fn(
                conn, since=month_start, until=today_end, interval=monthly_interval,
                group_by=group_by),
            "hourly_timeline": timeline_fn(
                conn, since=today_start, until=today_end, interval="hour", group_by=group_by),
        }

    # 함수는 이름으로(모듈 전역) 참조해 호출 — 모듈 상수로 캡처하면 patch(테스트) 가 안 먹으므로
    # 여기서 직접 넘겨 call-time 에 전역 조회되게 한다. group_by 는 각 timeline 화이트리스트 내.
    summary: dict[str, Any] = {
        "access": _domain_slice(access_log_stats, access_log_timeline, "action"),
        "lineage": _domain_slice(lineage_stats, lineage_timeline, "activity"),
        "asset": _domain_slice(asset_stats, asset_timeline, "modality"),
    }
    summary["meta"] = {
        "months": months,
        "monthly_interval": monthly_interval,  # 월별 슬라이스 버킷 단위 — 화면이 다시 묶을지 판단하는 근거
        "today_from": today_start.isoformat(),
        "today_to": today_end.isoformat(),
        "monthly_from": month_start.isoformat(),
        "generated_at": now.isoformat(),
    }
    return summary
