"""운영 대시보드 집계 — access·lineage·asset 3도메인을 한 트랜잭션에서 조합(013 운영 후속·052 번들).

배경(부하)
    관리자 대시보드(DataFlatformWeb)는 상세 화면 로더를 그대로 조합해 한 번 열 때 9~11회의
    ``/admin/*`` 호출을 낸다(도메인 3 × 전체/오늘/월별/시간별). Promise.all 병렬이라 체감은
    낫지만 HTTP 왕복·커넥션 풀 churn·모달리티 월별의 ``/assets`` 전수 스캔(N+1)이 그대로 누적된다.
    이 모듈은 검증된 순수 조회 함수 6종(``*_stats``/``*_timeline``)을 **한 커넥션·한 트랜잭션**에서
    조합해 단일 응답으로 내려, 왕복·풀 churn 을 없애고 프론트의 자산 전수 스캔을 제거한다.

설계
    - **재구현 0**: 기존 ``access_log``/``lineage_query``/``asset_stats`` 함수를 그대로 호출한다.
      의료(PHI) 제외·결정적 정렬·LLM 0 은 각 함수가 이미 보장하므로 조합 계층은 별도 정책이 없다.
    - **윈도우는 호출자가 준 ``now`` 로 계산**(핸들러가 ``datetime.now(UTC)`` 주입) — 대시보드의
      "오늘"·"최근 N개월"은 벽시계 상대라 결정성(헌법 3조)의 대상이 아니다(관계·검색 결정과 무관).
      now 를 인자로 받아 단위 테스트에서 고정 시각 주입이 가능하다(창 계산 결정적 검증).
    - group_by: access=action · lineage=activity · asset=modality (각 timeline 화이트리스트 내).
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

    예) day_start=2026-07-01, months=6 → 2026-02-01(2·3·4·5·6·7월 = 6개월). dateutil 없이
    월 경계로 정렬해 프론트의 월 롤업(일별 버킷→월 라벨)과 어긋나지 않게 한다.
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

    조회 전용·의료 제외(각 함수)·LLM 0·마이그레이션 0. 윈도우는 ``now`` 로 계산(주입·결정적 테스트).
    반환 ``{access:{...}, lineage:{...}, asset:{...}, meta:{...}}``.

    ``monthly_interval``(057 FR-303) — 월별 슬라이스 버킷 단위. 기본 ``"day"``(일별·하위호환·기존
    동작 완전 불변). ``"month"`` 면 월 버킷으로 내려 프론트의 일→월 롤업(``rollupTimelineSeriesToMonth``)을
    제거한다(각 timeline 함수의 TIMELINE_INTERVALS 화이트리스트·API 계층이 day|month 로 선검증). 시간별
    슬라이스(hourly_timeline)는 항상 hour 로 불변.
    """
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    month_start = _month_floor_back(today_start, months)

    def _domain_slice(stats_fn: Any, timeline_fn: Any, group_by: str) -> dict[str, Any]:
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
        "monthly_interval": monthly_interval,  # 057 FR-303: 월별 슬라이스 버킷 단위(프론트 롤업 여부 판단)
        "today_from": today_start.isoformat(),
        "today_to": today_end.isoformat(),
        "monthly_from": month_start.isoformat(),
        "generated_at": now.isoformat(),
    }
    return summary
