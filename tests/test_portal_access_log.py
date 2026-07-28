import unittest
from datetime import UTC, datetime, timezone

from service.portal.access_log import (
    access_log_overview,
    access_log_stats,
    access_log_timeline,
    derive_access_action,
    query_access_logs,
    record_access,
)


class _Cur:
    """execute 기록 + 미리 채운 결과를 순서대로 반환하는 fake 커서."""
    def __init__(self, results):
        self.calls = []
        self._results = list(results)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.calls.append((sql, params))
    def fetchone(self): return self._results.pop(0)
    def fetchall(self): return self._results.pop(0)


class _Conn:
    def __init__(self, results=()): self._cur = _Cur(results)
    def cursor(self): return self._cur


_UUID = "018f0000-0000-7000-8000-000000000251"  # UUID 형식 표본(B3: 단건 감사는 UUID 세그먼트만)


class DeriveActionTest(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(derive_access_action("GET", "/search"), ("search", None))
        self.assertEqual(derive_access_action("GET", f"/assets/{_UUID}"), ("asset_view", _UUID))
        self.assertEqual(derive_access_action("GET", f"/assets/{_UUID}/download"), ("download", _UUID))
        self.assertEqual(derive_access_action("GET", f"/assets/{_UUID}/bundle"), ("bundle", _UUID))

    def test_non_uuid_segment_none(self):
        # 2026-07-15 B3: 비-UUID 세그먼트(컬렉션/예약·오타)는 단건 감사 아님 — 과거엔 'unclassified' 를
        # asset_id 로 오인해 UUID FK INSERT 가 매번 실패(감사 유실+노이즈)했다. None=기록 안 함이 계약.
        for p in ("/assets/unclassified", "/assets/abc", "/assets/abc/download", "/assets/abc/bundle"):
            self.assertIsNone(derive_access_action("GET", p), p)

    def test_non_data_routes_none(self):
        for p in ("/health", "/me", "/auth/token", "/admin/access-logs", "/admin/access-logs/stats",
                  "/admin/access-logs/timeline", "/admin/lineage", "/admin/asset-stats", "/admin/assets",
                  "/admin/assets/abc/lineage", "/assets/"):
            self.assertIsNone(derive_access_action("GET", p), p)
        self.assertIsNone(derive_access_action("POST", "/search"))  # 비 GET


class RecordAccessTest(unittest.TestCase):
    def test_inserts_one_row_with_uuid(self):
        conn = _Conn()
        aid = record_access(conn, action="search", user_id="u1")
        self.assertTrue(aid)  # access_id 반환
        self.assertEqual(len(conn._cur.calls), 1)
        sql, params = conn._cur.calls[0]
        self.assertIn("INSERT INTO access_log", sql)
        self.assertEqual(params[2], "u1")     # user_id
        self.assertEqual(params[3], "search")  # action


class QueryStatsShapeTest(unittest.TestCase):
    def test_query_shape(self):
        ts = datetime(2026, 6, 30, tzinfo=UTC)
        conn = _Conn([(2,), [("id1", "search", "u1", None, ts), ("id2", "asset_view", "u1", "a9", ts)]])
        out = query_access_logs(conn, user_id="u1")
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["limit"], 50)   # FR-701: 페이징 봉투(맨앞/맨끝 이동)
        self.assertEqual(out["offset"], 0)
        self.assertEqual(out["rows"][0]["action"], "search")
        self.assertEqual(out["rows"][1]["asset_id"], "a9")

    def test_stats_shape(self):
        conn = _Conn([(3,), [("search", 2), ("asset_view", 1)], [("u1", 3)]])
        out = access_log_stats(conn)
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["by_action"][0], {"action": "search", "count": 2})
        self.assertEqual(out["by_user"][0], {"user_id": "u1", "count": 3})


class TimelineShapeTest(unittest.TestCase):
    def test_bucket_shape(self):
        b0 = datetime(2026, 6, 29, tzinfo=UTC)
        b1 = datetime(2026, 6, 30, tzinfo=UTC)
        # timeline 은 GROUP BY 단일 execute → fetchall 1회
        conn = _Conn([[(b0, 2), (b1, 5)]])
        out = access_log_timeline(conn)
        self.assertEqual(out["interval"], "day")
        self.assertEqual(out["buckets"][0], {"bucket": b0.isoformat(), "count": 2})
        self.assertEqual(out["buckets"][1], {"bucket": b1.isoformat(), "count": 5})

    def test_interval_whitelist_fallback(self):
        # 화이트리스트 밖 interval("year")은 day 로 폴백(인젝션 방지)
        conn = _Conn([[]])
        out = access_log_timeline(conn, interval="year")
        self.assertEqual(out["interval"], "day")
        sql = conn._cur.calls[0][0]
        self.assertIn("date_trunc('day'", sql)
        self.assertNotIn("year", sql)

    def test_interval_hour_passthrough(self):
        # 화이트리스트 안 interval("hour")은 그대로 사용
        conn = _Conn([[]])
        out = access_log_timeline(conn, interval="hour")
        self.assertEqual(out["interval"], "hour")
        self.assertIn("date_trunc('hour'", conn._cur.calls[0][0])

    def test_interval_month_passthrough(self):
        # 054 FR-401: month 화이트리스트 추가 — date_trunc('month') 사용
        conn = _Conn([[]])
        out = access_log_timeline(conn, interval="month")
        self.assertEqual(out["interval"], "month")
        self.assertIn("date_trunc('month'", conn._cur.calls[0][0])

    def test_action_filter_in_where(self):
        conn = _Conn([[]])
        access_log_timeline(conn, action="search")
        sql, params = conn._cur.calls[0]
        self.assertIn("action = %s", sql)
        self.assertIn("search", params)

    def test_group_by_action_multi_series(self):
        ts = datetime(2026, 6, 30, tzinfo=UTC)
        # group_by 시 (key, bucket, count) 행 → 멀티시리즈 피벗
        conn = _Conn([[("search", ts, 5), ("download", ts, 2)]])
        out = access_log_timeline(conn, group_by="action")
        self.assertEqual(out["group_by"], "action")
        keys = [s["key"] for s in out["series"]]
        self.assertEqual(keys, ["search", "download"])
        self.assertEqual(out["series"][0]["buckets"][0]["count"], 5)
        # group_by 컬럼은 화이트리스트 매핑(action)이라 SQL 에 안전 삽입
        self.assertIn("action AS key", conn._cur.calls[0][0])

    def test_group_by_unknown_falls_back_to_single_series(self):
        # 화이트리스트 밖 group_by 는 단일 시리즈로 폴백(인젝션 방지·API 레이어는 422 선처리)
        conn = _Conn([[]])
        out = access_log_timeline(conn, group_by="evil; DROP TABLE")
        self.assertIn("buckets", out)
        self.assertNotIn("series", out)


class AccessLogOverviewTest(unittest.TestCase):
    """057 FR-301 — access-logs overview BFF(stats + timeline 를 한 트랜잭션 조합·1회 응답).

    프론트가 stats+list+timeline 3회 순차 호출하던 것을 stats+timeline 1회로 묶는다(list 는 별도 페이징).
    검증된 순수 조회 함수 2종을 재사용해 재구현 0·의료 무관(access_log 는 자산 도메인 아님)·LLM 0.
    """

    def _conn(self, total, by_action, timeline_rows):
        # access_log_stats: COUNT(fetchone) → by_action(fetchall) → by_user(fetchall)
        # access_log_timeline(group_by=action): grouped rows(fetchall)
        return _Conn([(total,), by_action, [("u1", total)], timeline_rows])

    def test_overview_shape_total_by_action_timeline(self):
        ts = datetime(2026, 6, 30, tzinfo=UTC)
        conn = self._conn(3, [("search", 2), ("asset_view", 1)],
                          [("search", ts, 2), ("asset_view", ts, 1)])
        out = access_log_overview(conn)
        self.assertEqual(out["total"], 3)
        self.assertEqual(out["by_action"][0], {"action": "search", "count": 2})
        self.assertEqual(out["timeline"]["group_by"], "action")
        self.assertEqual([s["key"] for s in out["timeline"]["series"]], ["search", "asset_view"])

    def test_overview_action_scopes_timeline_only(self):
        # action 은 timeline 을 드릴다운(단일 action 시리즈)·stats(total/by_action)는 기간 전체 KPI.
        ts = datetime(2026, 6, 30, tzinfo=UTC)
        conn = self._conn(3, [("search", 2)], [("search", ts, 2)])
        access_log_overview(conn, action="search")
        calls = conn._cur.calls
        # 마지막 execute(timeline)에만 action=%s 바인딩, stats 3개 execute 엔 없음.
        stats_sqls = [sql for sql, _p in calls[:3]]
        timeline_sql, timeline_params = calls[3]
        for s in stats_sqls:
            self.assertNotIn("action = %s", s)
        self.assertIn("action = %s", timeline_sql)
        self.assertIn("search", timeline_params)

    def test_overview_interval_passthrough(self):
        conn = self._conn(0, [], [])
        out = access_log_overview(conn, interval="month")
        self.assertEqual(out["timeline"]["interval"], "month")
        self.assertIn("date_trunc('month'", conn._cur.calls[3][0])

    def test_overview_period_bound_on_all_queries(self):
        dt1 = datetime(2026, 6, 1, tzinfo=UTC)
        dt2 = datetime(2026, 6, 30, tzinfo=UTC)
        conn = self._conn(0, [], [])
        access_log_overview(conn, since=dt1, until=dt2)
        for sql, params in conn._cur.calls:
            self.assertIn("occurred_at >= %s", sql)
            self.assertIn("occurred_at < %s", sql)
            self.assertIn(dt1, params)
            self.assertIn(dt2, params)


if __name__ == "__main__":
    unittest.main()
