import unittest
from datetime import datetime, timezone

from service.portal.asset_stats import (
    _PROCESSING_STATUSES,
    _RELATION_PROPOSED_ACTIVITY,
    _SNAPSHOT_BUCKETS,
    _snapshot_bucket_predicate,
    asset_stats,
    asset_timeline,
    build_modality_overview,
    modality_detail,
    query_assets,
)


class _Cur:
    """execute 기록 + 미리 채운 결과를 순서대로 반환하는 fake 커서.

    COUNT(fetchone) → GROUP/SELECT(fetchall) 호출 순서대로 _results 를 소비한다.
    """
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


class AssetStatsShapeTest(unittest.TestCase):
    def test_stats_shape(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc).date()
        # COUNT → by_status → by_modality → by_domain → by_file_ext → by_date 순으로 소비(6 쿼리)
        conn = _Conn([
            (10,),
            [("registered", 7), ("failed", 3)],
            [("text", 6), ("image", 4)],
            [("general", 9), ("unknown", 1)],
            [("pdf", 5), ("txt", 4), (None, 1)],
            [(ts, 10)],
        ])
        out = asset_stats(conn)
        self.assertEqual(out["total"], 10)
        self.assertEqual(out["by_status"][0], {"status": "registered", "count": 7})
        self.assertEqual(out["by_modality"][0], {"modality": "text", "count": 6})
        self.assertEqual(out["by_domain"][0], {"domain": "general", "count": 9})
        self.assertEqual(out["by_file_ext"][0], {"file_ext": "pdf", "count": 5})
        self.assertIsNone(out["by_file_ext"][2]["file_ext"])  # 확장자 없음(NULL)
        self.assertEqual(out["by_date"][0], {"date": ts.isoformat(), "count": 10})

    def test_no_domain_exclusion_in_queries(self):
        # 2026-07-23: 도메인 제외 전면 제거 — 6개 SQL 어디에도 medical 배제가 없다.
        conn = _Conn([(0,), [], [], [], [], []])
        asset_stats(conn)
        self.assertEqual(len(conn._cur.calls), 6)
        for sql, _params in conn._cur.calls:
            self.assertNotIn("medical", sql)

    def test_deterministic_order_sql(self):
        conn = _Conn([(0,), [], [], [], [], []])
        asset_stats(conn)
        self.assertIn("ORDER BY COUNT(*) DESC, status ASC", conn._cur.calls[1][0])
        self.assertIn("ORDER BY COUNT(*) DESC, modality ASC", conn._cur.calls[2][0])
        self.assertIn("ORDER BY COUNT(*) DESC, domain_label ASC", conn._cur.calls[3][0])
        self.assertIn("ORDER BY COUNT(*) DESC, ext ASC NULLS LAST", conn._cur.calls[4][0])  # file_ext
        self.assertIn("GROUP BY d ORDER BY d ASC", conn._cur.calls[5][0])  # date 시간순

    def test_period_filter_in_all_queries(self):
        # from/to(생성일 기준·to exclusive) — 6개 집계 모두 기간 반영(프론트 ② 기간별 by_file_ext).
        # dt1<dt2 구간으로 호출(공집합 경계 dt==dt 회피)·파라미터 순서는 [since, until].
        dt1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([(0,), [], [], [], [], []])
        asset_stats(conn, since=dt1, until=dt2)
        self.assertEqual(len(conn._cur.calls), 6)
        for sql, params in conn._cur.calls:
            self.assertIn("created_at >= %s", sql)
            self.assertIn("created_at < %s", sql)
            self.assertNotIn("medical", sql)  # 2026-07-23: 도메인 제외 전면 제거
            self.assertEqual(params, [dt1, dt2])


class AssetStatsSnapshotBucketsTest(unittest.TestCase):
    """054 G2 — asset_stats by_snapshot_bucket 집계(FR-201/202)."""

    def _conn_with_buckets(self, total, filter_row):
        # 기존 6쿼리(COUNT→by_status→modality→domain→file_ext→date) 뒤에 단일 FILTER 쿼리(5 count)
        return _Conn([
            (total,),
            [],  # by_status
            [],  # by_modality
            [],  # by_domain
            [],  # by_file_ext
            [],  # by_date
            filter_row,  # FILTER 쿼리 fetchone → (processing, deferred, registered, failed, relation_proposed)
        ])

    def test_snapshot_buckets_shape_and_order(self):
        # FILTER 행 순서 = SELECT 리스트 순서(processing, deferred, registered, failed, relation_proposed)
        # 응답의 by_snapshot_bucket 은 _SNAPSHOT_BUCKETS 순서로 재배치(0 포함 5개)
        conn = self._conn_with_buckets(20, (3, 2, 10, 1, 4))
        out = asset_stats(conn, snapshot_buckets=True)
        self.assertIn("by_snapshot_bucket", out)
        buckets = out["by_snapshot_bucket"]
        self.assertEqual([b["bucket"] for b in buckets], list(_SNAPSHOT_BUCKETS))
        self.assertEqual(len(buckets), 5)
        as_dict = {b["bucket"]: b["count"] for b in buckets}
        self.assertEqual(as_dict["processing"], 3)
        self.assertEqual(as_dict["deferred"], 2)
        self.assertEqual(as_dict["registered"], 10)
        self.assertEqual(as_dict["failed"], 1)
        self.assertEqual(as_dict["relation_proposed"], 4)

    def test_snapshot_buckets_zero_included(self):
        # 0건 버킷도 응답에 항상 포함(5개 고정)
        conn = self._conn_with_buckets(0, (0, 0, 0, 0, 0))
        out = asset_stats(conn, snapshot_buckets=True)
        self.assertEqual(len(out["by_snapshot_bucket"]), 5)
        for b in out["by_snapshot_bucket"]:
            self.assertEqual(b["count"], 0)

    def test_sum_of_buckets_equals_total(self):
        # sum(by_snapshot_bucket) == total (동일 스코프 _period_clause)
        conn = self._conn_with_buckets(20, (3, 2, 10, 1, 4))
        out = asset_stats(conn, snapshot_buckets=True)
        self.assertEqual(sum(b["count"] for b in out["by_snapshot_bucket"]), out["total"])

    def test_filter_query_shape_and_binding(self):
        # 단일 FILTER 쿼리: 5 count·의료 제외 서브쿼리·activity=%s 바인딩
        conn = self._conn_with_buckets(20, (3, 2, 10, 1, 4))
        asset_stats(conn, snapshot_buckets=True)
        # 기존 6쿼리 + FILTER 쿼리 1개 = 7 execute
        self.assertEqual(len(conn._cur.calls), 7)
        filter_sql, filter_params = conn._cur.calls[6]
        self.assertEqual(filter_sql.count("FILTER"), 5)  # 5버킷 count
        self.assertNotIn("medical", filter_sql)  # 2026-07-23: 도메인 제외 전면 제거
        self.assertIn("asset_lineage l", filter_sql)  # relation_proposed EXISTS
        self.assertIn("l.activity = %s", filter_sql)
        # activity 파라미터가 첫 번째(SELECT 리스트 EXISTS 가 WHERE 보다 먼저 등장)
        self.assertEqual(filter_params, [_RELATION_PROPOSED_ACTIVITY])

    def test_filter_query_period_binding_order(self):
        # 기간 지정 시 params = [activity, since, until] (SELECT EXISTS 먼저, WHERE 기간 뒤)
        dt1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = self._conn_with_buckets(20, (3, 2, 10, 1, 4))
        asset_stats(conn, since=dt1, until=dt2, snapshot_buckets=True)
        filter_sql, filter_params = conn._cur.calls[6]
        self.assertIn("created_at >= %s", filter_sql)
        self.assertIn("created_at < %s", filter_sql)
        self.assertEqual(filter_params, [_RELATION_PROPOSED_ACTIVITY, dt1, dt2])

    def test_no_snapshot_buckets_keeps_legacy_shape(self):
        # 하위호환: snapshot_buckets 미지정(기본 False)이면 by_snapshot_bucket 없음·기존 6키만·6쿼리
        conn = _Conn([(0,), [], [], [], [], []])
        out = asset_stats(conn)
        self.assertNotIn("by_snapshot_bucket", out)
        self.assertEqual(
            set(out.keys()),
            {"total", "by_status", "by_modality", "by_domain", "by_file_ext", "by_date"})
        self.assertEqual(len(conn._cur.calls), 6)  # FILTER 쿼리 미실행


class QueryAssetsShapeTest(unittest.TestCase):
    def test_rows_shape_and_basename(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([
            (2,),
            [
                ("a1", "registered", "text", "general", "/data/raw/문서1.pdf", ts, "pdf"),
                ("a2", "failed", "image", "general", "/data/raw/사진.png", ts, "png"),
            ],
        ])
        out = query_assets(conn)
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["limit"], 50)   # FR-701: 페이징 봉투(맨앞/맨끝 이동)
        self.assertEqual(out["offset"], 0)
        self.assertEqual(out["rows"][0]["asset_id"], "a1")
        self.assertEqual(out["rows"][0]["status"], "registered")
        self.assertEqual(out["rows"][0]["modality"], "text")
        self.assertEqual(out["rows"][0]["domain_label"], "general")
        # file_name 은 fs_path 의 basename
        self.assertEqual(out["rows"][0]["file_name"], "문서1.pdf")
        self.assertEqual(out["rows"][1]["file_name"], "사진.png")
        self.assertEqual(out["rows"][0]["created_at"], ts.isoformat())

    def test_null_fs_path_file_name_none(self):
        conn = _Conn([(1,), [("a1", "registered", "text", "general", None, None, None)]])
        out = query_assets(conn)
        self.assertIsNone(out["rows"][0]["file_name"])
        self.assertIsNone(out["rows"][0]["created_at"])
        self.assertIsNone(out["rows"][0]["file_ext"])  # 확장자 없음(NULL)

    def test_rows_include_file_ext_from_by_file_ext_logic(self):
        # FR-104(057): 목록 행에 file_ext 하향(하위호환) — by_file_ext 집계와 동일 SQL(_EXT_EXPR) 파생
        # 이라 행의 file_ext == 집계 버킷 키(프론트 파일명 파싱·폴백 확장자 집계 제거).
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([
            (2,),
            [
                ("a1", "registered", "text", "general", "/data/raw/문서1.pdf", ts, "pdf"),
                ("a2", "failed", "image", "general", "/data/raw/사진.png", ts, "png"),
            ],
        ])
        out = query_assets(conn)
        self.assertEqual(out["rows"][0]["file_ext"], "pdf")
        self.assertEqual(out["rows"][1]["file_ext"], "png")
        # SELECT 는 by_file_ext 와 동일한 확장자 파생식(fs_path substring)을 file_ext 로 노출
        select_sql = conn._cur.calls[1][0]
        self.assertIn("substring(fs_path from", select_sql)
        self.assertIn("AS file_ext", select_sql)

    def test_no_domain_exclusion(self):
        # 2026-07-23: 도메인 제외 전면 제거 — query_assets SQL 에 medical 배제 없음.
        conn = _Conn([(0,), []])
        query_assets(conn)
        for sql, _params in conn._cur.calls:
            self.assertNotIn("medical", sql)

    def test_filters_in_where_and_params(self):
        conn = _Conn([(0,), []])
        query_assets(conn, status="registered", modality="text", domain="general")
        count_sql, count_params = conn._cur.calls[0]
        select_sql, select_params = conn._cur.calls[1]
        # 필터는 %s 바인딩으로 WHERE 에 들어가고, 의료 제외는 항상 포함
        self.assertIn("status = %s", count_sql)
        self.assertIn("modality = %s", count_sql)
        self.assertIn("domain_label = %s", count_sql)
        self.assertNotIn("<> 'medical'", count_sql)  # 2026-07-23: 도메인 제외 전면 제거
        self.assertEqual(count_params, ["registered", "text", "general"])
        # SELECT 도 동일 필터 + limit/offset 바인딩
        self.assertEqual(select_params, ["registered", "text", "general", 50, 0])

    def test_file_ext_and_date_filters(self):
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        conn = _Conn([(0,), []])
        query_assets(conn, file_ext="pdf", created_from=dt)
        count_sql, count_params = conn._cur.calls[0]
        # file_ext 은 fs_path 확장자 식 = %s, 날짜는 created_at >= %s, 의료 제외 항상 포함
        self.assertIn("substring(a.fs_path from", count_sql)  # 별칭 통일로 COUNT 도 a. 한정
        self.assertIn("created_at >= %s", count_sql)
        self.assertNotIn("medical", count_sql)  # 2026-07-23: 도메인 제외 전면 제거
        self.assertEqual(count_params, ["pdf", dt])

    def test_domain_medical_filter_returns_medical(self):
        # 2026-07-23: 도메인 제외 전면 제거 — domain='medical' 요청 시 medical 자산이 조회된다(배제 없음).
        conn = _Conn([(0,), []])
        query_assets(conn, domain="medical")
        count_sql, count_params = conn._cur.calls[0]
        self.assertIn("domain_label = %s", count_sql)
        self.assertNotIn("<> 'medical'", count_sql)  # 배제 절 없음
        self.assertEqual(count_params, ["medical"])

    def test_deterministic_order_sql(self):
        conn = _Conn([(0,), []])
        query_assets(conn)
        select_sql = conn._cur.calls[1][0]
        # created_at DESC + asset_id DESC tiebreak(결정적), 페이징 바인딩
        self.assertIn("ORDER BY created_at DESC, asset_id DESC LIMIT %s OFFSET %s", select_sql)

    def test_limit_offset_passthrough(self):
        conn = _Conn([(0,), []])
        query_assets(conn, limit=10, offset=20)
        _select_sql, select_params = conn._cur.calls[1]
        self.assertEqual(select_params, [10, 20])


class QueryAssetsContentTest(unittest.TestCase):
    """with_content=True — 모달리티 상세 목록에 요약·키워드·제목(파일명) 동반(보완 v6)."""
    def test_with_content_joins_metadata_and_adds_fields(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([
            (1,),
            [("a1", "registered", "video", "general", "/data/raw/뉴스.mp4", ts,
              "서울시장 선거 여론조사 보도", ["선거", "여론조사"], "mp4")],
        ])
        out = query_assets(conn, modality="video", with_content=True)
        row = out["rows"][0]
        self.assertEqual(row["file_name"], "뉴스.mp4")  # 제목=파일명
        self.assertEqual(row["summary"], "서울시장 선거 여론조사 보도")
        self.assertEqual(row["keywords"], ["선거", "여론조사"])
        self.assertEqual(row["file_ext"], "mp4")  # FR-104: content 경로도 file_ext 하향
        # content SELECT 는 asset_metadata LEFT JOIN + ext_meta 요약/키워드
        select_sql = conn._cur.calls[1][0]
        self.assertIn("LEFT JOIN asset_metadata", select_sql)
        self.assertIn("ext_meta", select_sql)
        # JOIN 시 asset_id 는 a. 한정(모호성 방지)
        self.assertIn("ORDER BY a.created_at DESC, a.asset_id DESC", select_sql)

    def test_without_content_keeps_lean_shape(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([(1,), [("a1", "registered", "text", "general", "/d/x.txt", ts, "txt")]])
        out = query_assets(conn)  # 기본 with_content=False — 하위호환(요약/키워드 없음)
        self.assertNotIn("summary", out["rows"][0])
        self.assertNotIn("keywords", out["rows"][0])
        self.assertNotIn("LEFT JOIN asset_metadata", conn._cur.calls[1][0])

    def test_with_content_no_domain_exclusion(self):
        # 2026-07-23: 도메인 제외 전면 제거 — content 경로도 medical 배제 없음.
        conn = _Conn([(0,), []])
        query_assets(conn, with_content=True)
        for sql, _p in conn._cur.calls:
            self.assertNotIn("medical", sql)

    def test_with_content_with_date_qualifies_created_at(self):
        # 🔴 회귀 가드: with_content JOIN + 날짜 필터 시 created_at 모호성(asset·asset_metadata 양쪽 보유)
        # → content SELECT 는 a. 한정해야 PG 오류 없음. 2026-07-23: COUNT·경량목록도 asset a 별칭 통일(a. 한정).
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        conn = _Conn([(0,), []])
        query_assets(conn, with_content=True, created_from=dt, created_to=dt)
        count_sql, select_sql = conn._cur.calls[0][0], conn._cur.calls[1][0]
        self.assertIn("LEFT JOIN asset_metadata", select_sql)
        self.assertIn("a.created_at >= %s", select_sql)  # JOIN 경로 한정
        self.assertIn("a.created_at < %s", select_sql)
        self.assertNotIn("LEFT JOIN", count_sql)  # COUNT 은 JOIN 불요(경량)
        self.assertIn("a.created_at >= %s", count_sql)  # 별칭 통일로 COUNT 도 a. 한정(EXISTS 상관 봉인)
        self.assertNotIn("medical", count_sql)  # 도메인 제외 전면 제거


class SnapshotBucketPredicateTest(unittest.TestCase):
    """054 G1 — 스냅샷 버킷 술어 헬퍼 + 상수(FR-101~104/601)."""

    def test_constants(self):
        # 버킷 5종 순서·processing 4status(classified 없음·status 아님)·relation activity
        self.assertEqual(
            _SNAPSHOT_BUCKETS,
            ("processing", "deferred", "registered", "failed", "relation_proposed"))
        self.assertEqual(
            _PROCESSING_STATUSES, ("received", "routing", "classifying", "extracting"))
        self.assertNotIn("classified", _PROCESSING_STATUSES)  # C1: classified 는 status 가 아님
        self.assertEqual(_RELATION_PROPOSED_ACTIVITY, "relations.proposed.v1")

    def test_processing_predicate(self):
        frag, params = _snapshot_bucket_predicate(
            "processing", "", relation_scope="period", since=None, until=None)
        self.assertIn("status IN (", frag)
        for st in _PROCESSING_STATUSES:
            self.assertIn(f"'{st}'", frag)
        self.assertEqual(params, [])  # status 집합은 고정 리터럴·파라미터 없음

    def test_deferred_predicate(self):
        frag, params = _snapshot_bucket_predicate(
            "deferred", "", relation_scope="period", since=None, until=None)
        self.assertIn("status = 'deferred'", frag)
        self.assertNotIn("EXISTS", frag)
        self.assertEqual(params, [])

    def test_failed_predicate(self):
        frag, params = _snapshot_bucket_predicate(
            "failed", "", relation_scope="period", since=None, until=None)
        self.assertIn("status = 'failed'", frag)
        self.assertNotIn("EXISTS", frag)
        self.assertEqual(params, [])

    def test_relation_proposed_predicate_alltime(self):
        # alltime → EXISTS 에 occurred_at 기간 조건 없음, activity 만 %s
        frag, params = _snapshot_bucket_predicate(
            "relation_proposed", "", relation_scope="alltime",
            since="X", until="Y")
        self.assertIn("status = 'registered'", frag)
        self.assertIn("EXISTS (SELECT 1 FROM asset_lineage l", frag)
        self.assertIn("l.asset_id = asset_id", frag)
        self.assertIn("l.activity = %s", frag)
        self.assertNotIn("occurred_at", frag)  # alltime 은 기간 없음
        self.assertNotIn("NOT EXISTS", frag)
        self.assertEqual(params, [_RELATION_PROPOSED_ACTIVITY])  # activity 만

    def test_relation_proposed_predicate_period_scoped(self):
        # period + since/until → EXISTS 에 occurred_at 기간(%s,%s), 파라미터 activity→since→until
        frag, params = _snapshot_bucket_predicate(
            "relation_proposed", "", relation_scope="period",
            since="S", until="U")
        self.assertIn("status = 'registered'", frag)
        self.assertIn("l.activity = %s", frag)
        self.assertIn("l.occurred_at >= %s", frag)
        self.assertIn("l.occurred_at < %s", frag)
        self.assertEqual(params, [_RELATION_PROPOSED_ACTIVITY, "S", "U"])

    def test_relation_proposed_period_without_range_is_unscoped(self):
        # period 이지만 since/until 없으면 기간 조건 없음(activity 만 바인딩)
        frag, params = _snapshot_bucket_predicate(
            "relation_proposed", "", relation_scope="period",
            since=None, until=None)
        self.assertNotIn("occurred_at", frag)
        self.assertEqual(params, [_RELATION_PROPOSED_ACTIVITY])

    def test_registered_predicate_uses_not_exists(self):
        frag, params = _snapshot_bucket_predicate(
            "registered", "", relation_scope="alltime", since=None, until=None)
        self.assertIn("status = 'registered'", frag)
        self.assertIn("NOT EXISTS (SELECT 1 FROM asset_lineage l", frag)
        self.assertIn("l.activity = %s", frag)
        self.assertEqual(params, [_RELATION_PROPOSED_ACTIVITY])

    def test_predicate_prefix_applied(self):
        # pfx="a." → asset 컬럼은 a. 한정(EXISTS 서브쿼리 l 은 자체 alias·모호성 없음)
        frag, _params = _snapshot_bucket_predicate(
            "relation_proposed", "a.", relation_scope="period",
            since="S", until="U")
        self.assertIn("a.status = 'registered'", frag)
        self.assertIn("l.asset_id = a.asset_id", frag)


class QueryAssetsSnapshotBucketTest(unittest.TestCase):
    """054 G1 — query_assets snapshot_bucket 통합(C3: 버킷 우선·하위호환)."""

    def test_relation_proposed_bucket_sql(self):
        dtx = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dty = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([(0,), []])
        # status 를 함께 줘도 snapshot_bucket 이 우선(C3) — status 스펙은 추가되지 않음
        query_assets(conn, snapshot_bucket="relation_proposed",
                     status="failed", created_from=dtx, created_to=dty)
        count_sql, count_params = conn._cur.calls[0]
        select_sql, _select_params = conn._cur.calls[1]
        # 버킷 술어가 SQL 에 반영: EXISTS·activity·registered
        self.assertIn("EXISTS (SELECT 1 FROM asset_lineage l", count_sql)
        self.assertIn("l.activity = %s", count_sql)
        self.assertIn("status = 'registered'", count_sql)
        self.assertIn("l.occurred_at >= %s", count_sql)  # period 기본 + 기간 있음
        self.assertIn("l.occurred_at < %s", count_sql)
        # C3: snapshot_bucket 우선 → status = %s 스펙은 추가되지 않음
        self.assertNotIn("status = %s", count_sql)
        # 결정적 정렬·의료 제외 유지
        self.assertIn("ORDER BY created_at DESC, asset_id DESC", select_sql)
        self.assertNotIn("medical", count_sql)  # 2026-07-23: 도메인 제외 전면 제거
        # 파라미터 순서 = WHERE 순서(specs 불변식): 의료 제외(무파라미터) →
        #   버킷 술어 EXISTS[activity, occurred_since, occurred_until] →
        #   자산 created 기간[created_from, created_to]. period 라 관계 occurred 기간도 스코프되고
        #   자산 created 기간 필터도 그대로 유지되어 총 5개(요청 §2.2: 두 기간 = 같은 값).
        # (status 는 C3 로 무시되므로 파라미터에 'failed' 없음)
        self.assertNotIn("failed", count_params)
        self.assertEqual(
            count_params, [_RELATION_PROPOSED_ACTIVITY, dtx, dty, dtx, dty])

    def test_relation_scope_default_is_period(self):
        # relation_scope 미지정 기본 'period' — 기간 있으면 occurred_at 스코프
        dtx = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dty = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([(0,), []])
        query_assets(conn, snapshot_bucket="relation_proposed",
                     created_from=dtx, created_to=dty)
        count_sql = conn._cur.calls[0][0]
        self.assertIn("l.occurred_at >= %s", count_sql)

    def test_relation_scope_alltime_drops_occurred_range(self):
        dtx = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dty = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([(0,), []])
        query_assets(conn, snapshot_bucket="relation_proposed",
                     relation_scope="alltime", created_from=dtx, created_to=dty)
        count_sql, count_params = conn._cur.calls[0]
        self.assertNotIn("l.occurred_at", count_sql)  # alltime → EXISTS 기간 없음
        # activity 만·이어서 자산 created_at 기간(>= <)은 여전히 자산 필터로 바인딩
        self.assertIn("created_at >= %s", count_sql)
        self.assertEqual(count_params, [_RELATION_PROPOSED_ACTIVITY, dtx, dty])

    def test_processing_bucket_sql(self):
        conn = _Conn([(0,), []])
        query_assets(conn, snapshot_bucket="processing")
        count_sql, count_params = conn._cur.calls[0]
        self.assertIn("status IN (", count_sql)
        self.assertIn("'received'", count_sql)
        self.assertNotIn("EXISTS", count_sql)
        self.assertEqual(count_params, [])  # 상태집합 리터럴·파라미터 없음

    def test_snapshot_bucket_none_keeps_legacy_sql(self):
        # 하위호환: snapshot_bucket 미지정 시 기존 동작·SQL 불변(status 스펙 정상 동작)
        conn = _Conn([(0,), []])
        query_assets(conn, status="registered")
        count_sql, count_params = conn._cur.calls[0]
        self.assertIn("status = %s", count_sql)
        self.assertNotIn("asset_lineage", count_sql)
        self.assertEqual(count_params, ["registered"])


class ModalityDetailTest(unittest.TestCase):
    """단일 모달리티 스코프 집계(보완 v6) — 확장자·상태·일자 + 총계(도메인 제외 없음)."""
    def test_shape_and_modality_bound(self):
        d = datetime(2026, 6, 30, tzinfo=timezone.utc).date()
        # COUNT → by_file_ext → by_status → by_date 순(4 쿼리)
        conn = _Conn([
            (9,),
            [("mp4", 7), ("mov", 2)],
            [("registered", 8), ("failed", 1)],
            [(d, 9)],
        ])
        out = modality_detail(conn, "video")
        self.assertEqual(out["modality"], "video")
        self.assertEqual(out["total"], 9)
        self.assertEqual(out["by_file_ext"][0], {"file_ext": "mp4", "count": 7})
        self.assertEqual(out["by_status"][0], {"status": "registered", "count": 8})
        self.assertEqual(out["by_date"][0], {"date": d.isoformat(), "count": 9})
        # modality 는 %s 바인딩(인젝션 안전)·4 쿼리 모두 의료 제외
        self.assertEqual(len(conn._cur.calls), 4)
        for sql, params in conn._cur.calls:
            self.assertNotIn("medical", sql)  # 2026-07-23: 도메인 제외 전면 제거
            self.assertIn("modality = %s", sql)
            self.assertEqual(params, ["video"])

    def test_deterministic_order(self):
        conn = _Conn([(0,), [], [], []])
        modality_detail(conn, "image")
        self.assertIn("ORDER BY COUNT(*) DESC, ext ASC NULLS LAST", conn._cur.calls[1][0])
        self.assertIn("ORDER BY COUNT(*) DESC, status ASC", conn._cur.calls[2][0])
        self.assertIn("GROUP BY d ORDER BY d ASC", conn._cur.calls[3][0])

    def test_period_filter(self):
        # 모달리티 드릴다운도 기간 필터(개요 from/to 와 일관·프론트 ② "모달리티 기간 조회").
        dt1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([(0,), [], [], []])
        modality_detail(conn, "video", since=dt1, until=dt2)
        for sql, params in conn._cur.calls:
            self.assertIn("created_at >= %s", sql)
            self.assertIn("created_at < %s", sql)
            self.assertEqual(params, ["video", dt1, dt2])  # modality 먼저, 기간 뒤


class AssetTimelineTest(unittest.TestCase):
    """자산 생성 일자 추이(보완 v6) — group_by 멀티시리즈(계보 timeline 과 동일 패턴)."""
    def test_single_series_default(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([[(ts, 5)]])
        out = asset_timeline(conn, interval="day")
        self.assertEqual(out["interval"], "day")
        self.assertEqual(out["buckets"][0]["count"], 5)
        self.assertNotIn("series", out)
        self.assertNotIn("medical", conn._cur.calls[0][0])  # 2026-07-23: 도메인 제외 전면 제거

    def test_group_by_modality_multiseries(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([[("image", ts, 4), ("video", ts, 2)]])
        out = asset_timeline(conn, group_by="modality")
        self.assertEqual(out["group_by"], "modality")
        self.assertEqual([s["key"] for s in out["series"]], ["image", "video"])
        self.assertEqual(out["series"][0]["buckets"][0]["count"], 4)
        sql = conn._cur.calls[0][0]
        self.assertIn("modality AS key", sql)  # 화이트리스트 매핑 컬럼
        self.assertIn("ORDER BY key ASC, bkt ASC", sql)  # 결정적

    def test_group_by_unknown_falls_back_single(self):
        conn = _Conn([[]])
        out = asset_timeline(conn, group_by="evil; DROP TABLE")
        self.assertIn("buckets", out)
        self.assertNotIn("series", out)

    def test_group_by_file_ext_uses_ext_expr(self):
        # 프론트 ③ 일별 파일 포맷 추이 — group_by=file_ext 면 확장자식이 시리즈 key
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        conn = _Conn([[("pdf", ts, 3), ("txt", ts, 1)]])
        out = asset_timeline(conn, group_by="file_ext")
        self.assertEqual(out["group_by"], "file_ext")
        self.assertEqual([s["key"] for s in out["series"]], ["pdf", "txt"])
        sql = conn._cur.calls[0][0]
        self.assertIn("substring(fs_path from", sql)  # 화이트리스트 매핑=확장자 정규식
        self.assertIn("ORDER BY key ASC, bkt ASC", sql)  # 결정적

    def test_bad_interval_falls_back_to_day(self):
        conn = _Conn([[]])
        out = asset_timeline(conn, interval="year")
        self.assertEqual(out["interval"], "day")  # 화이트리스트 폴백(API 는 422 선처리)

    def test_interval_month_passthrough(self):
        # 054 FR-401: month 지원(프론트 일→월 롤업 서버 이관)
        conn = _Conn([[]])
        out = asset_timeline(conn, interval="month")
        self.assertEqual(out["interval"], "month")

    def test_modality_filter_opt_in(self):
        # 057 FR-302: modality 필터(모달리티 현황 BFF timeline) — 지정 시 WHERE modality=%s·바인딩.
        conn = _Conn([[]])
        asset_timeline(conn, modality="video", interval="month")
        sql, params = conn._cur.calls[0]
        self.assertIn("modality = %s", sql)
        self.assertIn("video", params)
        self.assertNotIn("medical", sql)  # 2026-07-23: 도메인 제외 전면 제거

    def test_no_modality_keeps_legacy_shape(self):
        # 하위호환: modality 미지정(기본 None)이면 기존 SQL·동작 불변(modality 절 없음).
        conn = _Conn([[]])
        asset_timeline(conn, interval="day")
        sql, params = conn._cur.calls[0]
        self.assertNotIn("modality = %s", sql)
        self.assertEqual(params, [])


class BuildModalityOverviewTest(unittest.TestCase):
    """057 FR-302 — 모달리티 현황 BFF(modality_detail + asset_timeline + first-page 를 한 트랜잭션 조합)."""

    def test_overview_combines_three_slices(self):
        ts = datetime(2026, 6, 30, tzinfo=timezone.utc)
        d = ts.date()
        # 순서: modality_detail(COUNT→by_file_ext→by_status→by_date=4쿼리)
        #      → asset_timeline(단일 execute·1)
        #      → query_assets(COUNT→rows=2쿼리)
        conn = _Conn([
            (9,),                         # modality_detail COUNT
            [("mp4", 7), ("mov", 2)],     # by_file_ext
            [("registered", 9)],          # by_status
            [(d, 9)],                     # by_date
            [(ts, 9)],                    # asset_timeline buckets
            (9,),                         # query_assets COUNT
            [("a1", "registered", "video", "general", "/x/뉴스.mp4", ts, "요약", ["k"], "mp4")],  # rows(content)
        ])
        out = build_modality_overview(conn, "video", interval="month", limit=10)
        self.assertEqual(out["detail"]["modality"], "video")
        self.assertEqual(out["detail"]["total"], 9)
        self.assertEqual(out["timeline"]["interval"], "month")
        self.assertEqual(out["timeline"]["buckets"][0]["count"], 9)
        self.assertEqual(out["first_page"]["rows"][0]["file_name"], "뉴스.mp4")
        self.assertEqual(out["first_page"]["rows"][0]["summary"], "요약")  # with_content

    def test_overview_scopes_all_slices_to_modality(self):
        conn = _Conn([
            (0,), [], [], [],   # modality_detail
            [],                 # asset_timeline
            (0,), [],           # query_assets
        ])
        build_modality_overview(conn, "image")
        for sql, _p in conn._cur.calls:
            self.assertNotIn("medical", sql)  # 2026-07-23: 도메인 제외 전면 제거(모든 슬라이스)
        # asset_timeline 슬라이스(5번째 execute)에 modality=%s 스코프
        self.assertIn("modality = %s", conn._cur.calls[4][0])


if __name__ == "__main__":
    unittest.main()
