import unittest
from datetime import datetime, timezone

from service.portal.asset_stats import _RELATION_PROPOSED_ACTIVITY
from service.portal.lineage_query import (
    lineage_stats,
    lineage_timeline,
    query_asset_lineage,
    query_lineage_feed,
    relation_proposed_summary,
)


class _Cur:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.calls.append((sql, params))
    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, rows): self._cur = _Cur(rows)
    def cursor(self): return self._cur


class _SeqCur:
    """execute 기록 + 미리 채운 결과를 순서대로 반환하는 fake 커서(COUNT→rows 2회 fetch)."""
    def __init__(self, results):
        self.calls = []
        self._results = list(results)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.calls.append((sql, params))
    def fetchone(self): return self._results.pop(0)
    def fetchall(self): return self._results.pop(0)


class _SeqConn:
    def __init__(self, results=()): self._cur = _SeqCur(results)
    def cursor(self): return self._cur


class QueryLineageTest(unittest.TestCase):
    def test_shape_and_order_query(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        rows = [("ingest.received.v1", "run_ingest", {}, {}, ts),
                ("ingest.registered.v1", "run_ingest", {}, {"channels": 2}, ts)]
        out = query_asset_lineage(_Conn(rows), "a1")
        self.assertEqual(out[0]["activity"], "ingest.received.v1")
        self.assertEqual(out[1]["generated"], {"channels": 2})
        # 시간순 정렬 SQL 사용 확인
        conn = _Conn(rows)
        query_asset_lineage(conn, "a1")
        sql = conn._cur.calls[0][0]
        self.assertIn("ORDER BY al.occurred_at ASC", sql)
        self.assertIn("a.domain_label <> 'medical'", sql)  # 의료 제외 조인(헌법 10조)


class QueryLineageFeedTest(unittest.TestCase):
    def test_shape_and_total_no_filter(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        # feed 는 COUNT(*) 1행 → rows 목록, 2회 fetch
        conn = _SeqConn([(2,), [("l1", "a1", "ingest.received.v1", "run_ingest", ts),
                                ("l2", "a9", "ingest.registered.v1", "run_ingest", ts)]])
        out = query_lineage_feed(conn)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["limit"], 50)   # FR-701: 페이징 봉투(맨앞/맨끝 이동)
        self.assertEqual(out["offset"], 0)
        # rows 에 asset_id 포함(전 자산 피드)
        self.assertEqual(out["rows"][0]["asset_id"], "a1")
        self.assertEqual(out["rows"][1]["activity"], "ingest.registered.v1")
        self.assertEqual(out["rows"][0]["occurred_at"], ts.isoformat())

    def test_order_by_occurred_at_desc_sql(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _SeqConn([(1,), [("l1", "a1", "ingest.received.v1", "run_ingest", ts)]])
        query_lineage_feed(conn)
        # 두 번째 execute(rows 조회)에 시간역순·tiebreak 정렬 SQL 사용 확인
        rows_sql = conn._cur.calls[1][0]
        self.assertIn("ORDER BY al.occurred_at DESC, al.lineage_id DESC", rows_sql)
        self.assertIn("a.domain_label <> 'medical'", rows_sql)  # 의료 제외(헌법 10조)

    def test_activity_filter_in_where(self):
        conn = _SeqConn([(0,), []])
        query_lineage_feed(conn, activity="ingest.received.v1")
        # COUNT·rows 두 execute 모두 WHERE 에 activity 조건 + 바인딩 파라미터
        count_sql, count_params = conn._cur.calls[0]
        self.assertIn("al.activity = %s", count_sql)
        self.assertIn("ingest.received.v1", count_params)

    def test_asset_dimension_filters(self):
        # 자산 차원 필터(modality·status·file_ext)는 asset 조인(a)으로 WHERE 에 들어간다(대시보드 슬라이스).
        conn = _SeqConn([(0,), []])
        query_lineage_feed(conn, modality="video", status="registered", file_ext="mp4")
        count_sql, count_params = conn._cur.calls[0]
        self.assertIn("a.modality = %s", count_sql)
        self.assertIn("a.status = %s", count_sql)
        self.assertIn("substring(a.fs_path from", count_sql)  # file_ext=확장자
        self.assertIn("a.domain_label <> 'medical'", count_sql)  # 의료 제외 유지
        for v in ("video", "registered", "mp4"):
            self.assertIn(v, count_params)


class LineageStatsTest(unittest.TestCase):
    def test_shape_and_medical_excluded(self):
        d = datetime(2026, 6, 30, tzinfo=timezone.utc).date()
        # COUNT → by_activity → by_day → by_modality → by_status → by_file_ext (6 쿼리)
        conn = _SeqConn([
            (12,),
            [("ingest.registered.v1", 8), ("relations.proposed.v1", 4)],
            [(d, 12)],
            [("text", 7), ("video", 5)],
            [("registered", 10), ("failed", 2)],
            [("txt", 6), ("mp4", 5), (None, 1)],
        ])
        out = lineage_stats(conn)
        self.assertEqual(out["total"], 12)
        self.assertEqual(out["by_activity"][0], {"activity": "ingest.registered.v1", "count": 8})
        self.assertEqual(out["by_day"][0], {"day": d.isoformat(), "count": 12})
        self.assertEqual(out["by_modality"][0]["modality"], "text")
        self.assertEqual(out["by_status"][0]["status"], "registered")
        self.assertEqual(out["by_file_ext"][0]["file_ext"], "txt")
        # 6개 SQL 모두 의료 제외 조인 포함
        self.assertEqual(len(conn._cur.calls), 6)
        for sql, _p in conn._cur.calls:
            self.assertIn("a.domain_label <> 'medical'", sql)


class LineageTimelineTest(unittest.TestCase):
    def test_single_series_no_group_by(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        out = lineage_timeline(_Conn([(ts, 5)]), interval="day", group_by=None)
        self.assertEqual(out["interval"], "day")
        self.assertEqual(out["buckets"][0]["count"], 5)
        self.assertNotIn("series", out)

    def test_interval_month_passthrough(self):
        # 054 FR-401: month 화이트리스트 추가(프론트 일→월 롤업 서버 이관)
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        out = lineage_timeline(_Conn([(ts, 5)]), interval="month", group_by=None)
        self.assertEqual(out["interval"], "month")

    def test_multi_series_group_by_activity(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        # 그룹 행: (key, bucket, count) — key ASC·bucket ASC 정렬 가정
        conn = _Conn([
            ("ingest.received.v1", ts, 3),
            ("ingest.received.v1", ts, 2),
            ("ingest.registered.v1", ts, 4),
        ])
        out = lineage_timeline(conn, group_by="activity")
        self.assertEqual(out["group_by"], "activity")
        self.assertEqual(len(out["series"]), 2)  # 2개 활동 시리즈로 피벗
        self.assertEqual(out["series"][0]["key"], "ingest.received.v1")
        self.assertEqual(len(out["series"][0]["buckets"]), 2)
        self.assertEqual(out["series"][1]["key"], "ingest.registered.v1")
        # group_by 컬럼은 화이트리스트 매핑이라 SQL 에 al.activity 로 들어감(인젝션 안전)
        self.assertIn("al.activity AS key", conn._cur.calls[0][0])
        self.assertIn("a.domain_label <> 'medical'", conn._cur.calls[0][0])


class RelationProposedSummaryTest(unittest.TestCase):
    """057 FR-204 — relations.proposed distinct 자산 수 + 발생 추이(limit 캡 없는 서버 집계).

    admin 이 getLineageFeed(limit:200) 원시 피드를 프론트에서 distinct/버킷팅하던 것을 서버로 이관 —
    200 초과 과소집계 실버그를 COUNT(DISTINCT)·전기간 집계로 바로잡는다. occurred_at 기준·의료 제외.
    """

    def _conn(self, distinct, timeline_rows):
        # 1) COUNT(DISTINCT al.asset_id) → fetchone → (distinct,)
        # 2) timeline (bucket, count) 목록 → fetchall
        return _SeqConn([(distinct,), timeline_rows])

    def test_shape_distinct_and_timeline(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        out = relation_proposed_summary(self._conn(42, [(ts, 5), (ts, 3)]))
        self.assertEqual(out["distinct_assets"], 42)
        self.assertEqual(out["timeline"]["interval"], "day")
        self.assertEqual(out["timeline"]["buckets"][0], {"bucket": ts.isoformat(), "count": 5})
        self.assertEqual(out["timeline"]["buckets"][1]["count"], 3)

    def test_distinct_count_no_limit_cap(self):
        # 실버그 회귀 가드: distinct 는 COUNT(DISTINCT al.asset_id) 로 LIMIT 없이 전기간 집계.
        conn = self._conn(500, [])
        relation_proposed_summary(conn)
        count_sql = conn._cur.calls[0][0]
        self.assertIn("COUNT(DISTINCT al.asset_id)", count_sql)
        self.assertNotIn("LIMIT", count_sql.upper())

    def test_activity_bound_and_medical_excluded(self):
        conn = self._conn(0, [])
        relation_proposed_summary(conn)
        for sql, params in conn._cur.calls:
            self.assertIn("al.activity = %s", sql)
            self.assertIn("a.domain_label <> 'medical'", sql)  # 의료 제외 조인(헌법 10조)
            self.assertIn(_RELATION_PROPOSED_ACTIVITY, params)

    def test_timeline_distinct_assets_per_bucket_deterministic(self):
        # 추이 버킷도 distinct 자산 수(재실행 중복 제거)·bucket ASC(결정적).
        conn = self._conn(0, [])
        relation_proposed_summary(conn, interval="day")
        tl_sql = conn._cur.calls[1][0]
        self.assertIn("COUNT(DISTINCT al.asset_id)", tl_sql)
        self.assertIn("date_trunc('day', al.occurred_at)", tl_sql)
        self.assertIn("GROUP BY bkt ORDER BY bkt ASC", tl_sql)

    def test_interval_month_passthrough(self):
        conn = self._conn(0, [])
        out = relation_proposed_summary(conn, interval="month")
        self.assertEqual(out["timeline"]["interval"], "month")
        self.assertIn("date_trunc('month', al.occurred_at)", conn._cur.calls[1][0])

    def test_bad_interval_falls_back_to_day(self):
        conn = self._conn(0, [])
        out = relation_proposed_summary(conn, interval="year")
        self.assertEqual(out["timeline"]["interval"], "day")  # 화이트리스트 폴백(API 는 422 선처리)

    def test_period_filter_bound_on_occurred_at(self):
        dt1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = self._conn(0, [])
        relation_proposed_summary(conn, since=dt1, until=dt2)
        for sql, params in conn._cur.calls:
            self.assertIn("al.occurred_at >= %s", sql)
            self.assertIn("al.occurred_at < %s", sql)
            # activity 먼저, 기간 뒤(SQL 등장 순서 = 파라미터 순서)
            self.assertEqual(params, [_RELATION_PROPOSED_ACTIVITY, dt1, dt2])


if __name__ == "__main__":
    unittest.main()
