"""GET /admin/dashboard/summary 포탈 핸들러 단위 테스트 — DB·LLM·네트워크 불필요(013 운영 후속·052 번들).

FastAPI ``TestClient`` 로 라우팅·months 검증·위임·401 만 확인한다. 집계 조합
(``build_dashboard_summary``)과 DB seam(``_run_in_db``)을 patch 로 대체해 순수 단위로 돈다.
``tests/test_portal_api_relations.py`` 의 auth bypass + passthrough 패턴을 따른다.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app

_AUTH_DISABLED_ENV = {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"}


def _passthrough_db(callback):
    return callback(object())  # build_dashboard_summary 는 patch 되므로 conn 값은 무의미


def _enable_bypass(tc: unittest.TestCase) -> None:
    env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
    env.start()
    tc.addCleanup(env.stop)
    p = patch("service.api._infra._run_in_db", _passthrough_db)
    p.start()
    tc.addCleanup(p.stop)


_SENTINEL = {"access": {}, "lineage": {}, "asset": {}, "meta": {}}


class TestDashboardSummary(unittest.TestCase):
    def setUp(self) -> None:
        _enable_bypass(self)
        self.client = TestClient(app)

    @patch("service.api.routes_admin.build_dashboard_summary")
    def test_summary_200_passes_months_and_now(self, mock_build) -> None:
        mock_build.return_value = _SENTINEL
        resp = self.client.get("/admin/dashboard/summary", params={"months": 3})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), _SENTINEL)
        self.assertEqual(mock_build.call_args.kwargs["months"], 3)
        now = mock_build.call_args.kwargs["now"]
        self.assertIsInstance(now, datetime)
        self.assertIsNotNone(now.tzinfo)  # 서버 now 는 tz-aware(UTC)

    @patch("service.api.routes_admin.build_dashboard_summary")
    def test_summary_default_months_6(self, mock_build) -> None:
        mock_build.return_value = _SENTINEL
        resp = self.client.get("/admin/dashboard/summary")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_build.call_args.kwargs["months"], 6)

    @patch("service.api.routes_admin.build_dashboard_summary")
    def test_months_out_of_range_422(self, mock_build) -> None:
        for bad in (0, 25):
            resp = self.client.get("/admin/dashboard/summary", params={"months": bad})
            self.assertEqual(resp.status_code, 422, f"months={bad}")
        mock_build.assert_not_called()

    @patch("service.api.routes_admin.build_dashboard_summary")
    def test_monthly_interval_month_passthrough(self, mock_build) -> None:
        # 057 FR-303: monthly_interval=month 를 200 으로 허용·서비스에 전달(프론트 일→월 롤업 제거).
        mock_build.return_value = _SENTINEL
        resp = self.client.get("/admin/dashboard/summary", params={"monthly_interval": "month"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_build.call_args.kwargs["monthly_interval"], "month")

    @patch("service.api.routes_admin.build_dashboard_summary")
    def test_monthly_interval_default_day(self, mock_build) -> None:
        # 하위호환: 미지정 시 monthly_interval=day 로 전달(기존 동작 불변).
        mock_build.return_value = _SENTINEL
        resp = self.client.get("/admin/dashboard/summary")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_build.call_args.kwargs["monthly_interval"], "day")

    @patch("service.api.routes_admin.build_dashboard_summary")
    def test_monthly_interval_bad_value_422(self, mock_build) -> None:
        # 월별 슬라이스는 day|month 만 허용(hour 는 월 범위에 부적합) — 그 외 422.
        for bad in ("hour", "year"):
            resp = self.client.get("/admin/dashboard/summary", params={"monthly_interval": bad})
            self.assertEqual(resp.status_code, 422, f"monthly_interval={bad}")
        mock_build.assert_not_called()


class TestDashboardSummaryAuth(unittest.TestCase):
    """auth bypass 없이 — 토큰 없으면 401(require_principal)."""

    def setUp(self) -> None:
        # bypass 비활성(운영 모드) — 토큰 없는 요청은 401 이어야 한다.
        env = patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "0",
                                      "PORTAL_JWT_SECRET": "test-secret"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)

    def test_without_token_401(self) -> None:
        resp = self.client.get("/admin/dashboard/summary")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
