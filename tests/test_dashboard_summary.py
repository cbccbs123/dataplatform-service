"""build_dashboard_summary 단위 테스트 — 서비스 함수 조합·윈도우 계산·응답 조립(013 운영 후속·052 번들).

DB·LLM 불필요: 6개 순수 조회 함수(access/lineage/asset stats·timeline)를 patch 로 대체하고,
고정 ``now`` 를 주입해 창(오늘·최근 N개월) 계산과 도메인별 슬라이스 조립을 결정적으로 검증한다.
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime, timezone
from unittest.mock import MagicMock, patch

_NOW = datetime(2026, 7, 1, 15, 30, tzinfo=UTC)  # 고정 주입 시각(창 계산 결정적)


class TestBuildDashboardSummary(unittest.TestCase):
    def _run(self, months=6):
        from service.portal import dashboard
        conn = MagicMock()
        # 도메인·슬라이스 식별 가능한 sentinel 반환(조립 위치 검증용).
        with patch.object(dashboard, "access_log_stats", side_effect=lambda *a, **k: ("a_stats", k)) as a_s, \
             patch.object(dashboard, "access_log_timeline", side_effect=lambda *a, **k: ("a_tl", k)) as a_t, \
             patch.object(dashboard, "lineage_stats", side_effect=lambda *a, **k: ("l_stats", k)) as l_s, \
             patch.object(dashboard, "lineage_timeline", side_effect=lambda *a, **k: ("l_tl", k)) as l_t, \
             patch.object(dashboard, "asset_stats", side_effect=lambda *a, **k: ("as_stats", k)) as as_s, \
             patch.object(dashboard, "asset_timeline", side_effect=lambda *a, **k: ("as_tl", k)) as as_t:
            result = dashboard.build_dashboard_summary(conn, now=_NOW, months=months)
        return result, {
            "access_stats": a_s, "access_tl": a_t, "lineage_stats": l_s,
            "lineage_tl": l_t, "asset_stats": as_s, "asset_tl": as_t, "conn": conn}

    def test_three_domains_and_slices(self):
        result, _ = self._run()
        for domain in ("access", "lineage", "asset"):
            self.assertIn(domain, result)
            for slot in ("kpi_alltime", "kpi_today", "monthly_timeline", "hourly_timeline"):
                self.assertIn(slot, result[domain])
        self.assertIn("meta", result)

    def test_window_computation(self):
        # 2026-07-01 15:30 → today [07-01 00:00, 07-02 00:00), month_start 6개월 정렬=02-01.
        result, _ = self._run(months=6)
        meta = result["meta"]
        self.assertEqual(meta["today_from"], "2026-07-01T00:00:00+00:00")
        self.assertEqual(meta["today_to"], "2026-07-02T00:00:00+00:00")
        self.assertEqual(meta["monthly_from"], "2026-02-01T00:00:00+00:00")
        self.assertEqual(meta["months"], 6)
        self.assertEqual(meta["generated_at"], _NOW.isoformat())

    def test_month_floor_back_crosses_year(self):
        # 2026-07-01, months=12 → 2025-08-01(연 경계 넘김).
        result, _ = self._run(months=12)
        self.assertEqual(result["meta"]["monthly_from"], "2025-08-01T00:00:00+00:00")

    def test_stats_called_alltime_and_today(self):
        _, m = self._run()
        # 전체(kwargs 없음) + 오늘(since=today_start, until=today_end) 2회.
        self.assertEqual(m["access_stats"].call_count, 2)
        alltime_call, today_call = m["access_stats"].call_args_list
        self.assertEqual(alltime_call.kwargs, {})  # 전체 = 창 없음
        self.assertEqual(today_call.kwargs["since"].isoformat(), "2026-07-01T00:00:00+00:00")
        self.assertEqual(today_call.kwargs["until"].isoformat(), "2026-07-02T00:00:00+00:00")

    def test_timeline_monthly_daily_and_today_hourly_with_group_by(self):
        _, m = self._run()
        # access timeline 2회: 월별(일별·month_start~today_end)·오늘(시간별). group_by=action.
        self.assertEqual(m["access_tl"].call_count, 2)
        monthly, hourly = m["access_tl"].call_args_list
        self.assertEqual(monthly.kwargs["interval"], "day")
        self.assertEqual(monthly.kwargs["group_by"], "action")
        self.assertEqual(monthly.kwargs["since"].isoformat(), "2026-02-01T00:00:00+00:00")
        self.assertEqual(hourly.kwargs["interval"], "hour")
        self.assertEqual(hourly.kwargs["group_by"], "action")
        self.assertEqual(hourly.kwargs["since"].isoformat(), "2026-07-01T00:00:00+00:00")

    def test_domain_group_by_mapping(self):
        # lineage=activity, asset=modality 로 timeline 호출됨.
        _, m = self._run()
        self.assertEqual(m["lineage_tl"].call_args_list[0].kwargs["group_by"], "activity")
        self.assertEqual(m["asset_tl"].call_args_list[0].kwargs["group_by"], "modality")

    def test_monthly_interval_default_is_day(self):
        # 057 FR-303 하위호환: monthly_interval 미지정 → 기존대로 월별 시리즈=일별 버킷(프론트 무영향).
        _, m = self._run()
        self.assertEqual(m["access_tl"].call_args_list[0].kwargs["interval"], "day")
        self.assertEqual(m["asset_tl"].call_args_list[0].kwargs["interval"], "day")

    def test_monthly_interval_month_opt_in(self):
        # 057 FR-303: monthly_interval="month" → 월별 슬라이스가 month 버킷(프론트 rollupTimelineSeriesToMonth 제거).
        from service.portal import dashboard
        conn = MagicMock()
        with patch.object(dashboard, "access_log_stats", side_effect=lambda *a, **k: ("a_stats", k)), \
             patch.object(dashboard, "access_log_timeline", side_effect=lambda *a, **k: ("a_tl", k)) as a_t, \
             patch.object(dashboard, "lineage_stats", side_effect=lambda *a, **k: ("l_stats", k)), \
             patch.object(dashboard, "lineage_timeline", side_effect=lambda *a, **k: ("l_tl", k)), \
             patch.object(dashboard, "asset_stats", side_effect=lambda *a, **k: ("as_stats", k)), \
             patch.object(dashboard, "asset_timeline", side_effect=lambda *a, **k: ("as_tl", k)) as as_t:
            dashboard.build_dashboard_summary(conn, now=_NOW, months=6, monthly_interval="month")
        # 월별(첫 timeline 호출)만 month, 오늘 시간별(두 번째)은 여전히 hour.
        self.assertEqual(a_t.call_args_list[0].kwargs["interval"], "month")
        self.assertEqual(a_t.call_args_list[1].kwargs["interval"], "hour")
        self.assertEqual(as_t.call_args_list[0].kwargs["interval"], "month")


if __name__ == "__main__":
    unittest.main()
