import os
import unittest
from unittest import mock

os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")  # dev bypass(anonymous=public)

from fastapi.testclient import TestClient  # noqa: E402

from service.api import app, _infra, routes_admin, routes_assets  # noqa: E402  (레포 분리: 코어 src.app 제거·백엔드 app 직접 사용)


class HistoryEndpointsTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_lineage_endpoint(self):
        with mock.patch.object(routes_admin, "query_asset_lineage",
                               return_value=[{"activity": "ingest.received.v1", "agent": "run_ingest",
                                              "used": {}, "generated": {}, "occurred_at": "2026-06-30T00:00:00+00:00"}]), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/a1/lineage")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["activities"][0]["activity"], "ingest.received.v1")

    def test_access_logs_endpoint(self):
        with mock.patch.object(routes_admin, "query_access_logs",
                               return_value={"rows": [], "total": 0}), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs?action=search")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"rows": [], "total": 0})

    def test_stats_endpoint(self):
        with mock.patch.object(routes_admin, "access_log_stats",
                               return_value={"total": 0, "by_action": [], "by_user": []}), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs/stats")
        self.assertEqual(r.status_code, 200)
        self.assertIn("by_action", r.json())

    def test_access_logs_bad_date_returns_422(self):
        # _parse_dt: 잘못된 날짜 형식 → HTTPException(422)(헌법 8조·오류 경로 검증).
        r = self.client.get("/admin/access-logs?from=not-a-date")
        self.assertEqual(r.status_code, 422)

    def test_lineage_feed_endpoint(self):
        with mock.patch.object(routes_admin, "query_lineage_feed",
                               return_value={"rows": [{"lineage_id": "l1", "asset_id": "a1",
                                                       "activity": "ingest.registered.v1", "agent": "run_ingest",
                                                       "occurred_at": "2026-06-30T00:00:00+00:00"}], "total": 1}) as feed, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/lineage?limit=10&modality=video&status=registered&file_ext=mp4")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["rows"][0]["asset_id"], "a1")
        # 라우터→서비스 자산차원 필터 배선 검증(modality·status·file_ext 전달).
        kw = feed.call_args.kwargs
        self.assertEqual((kw["modality"], kw["status"], kw["file_ext"]), ("video", "registered", "mp4"))

    def test_timeline_endpoint(self):
        with mock.patch.object(routes_admin, "access_log_timeline",
                               return_value={"interval": "day", "buckets": [{"bucket": "2026-06-30T00:00:00+00:00", "count": 5}]}), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs/timeline?interval=day&action=search")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["buckets"][0]["count"], 5)

    def test_timeline_bad_interval_422(self):
        r = self.client.get("/admin/access-logs/timeline?interval=year")
        self.assertEqual(r.status_code, 422)

    def test_timeline_month_accepted_all_endpoints(self):
        # 054 FR-401: 3 timeline 엔드포인트가 interval=month 를 200 으로 허용해야 함(422 아님).
        # 서비스 화이트리스트에 month 추가됐어도 엔드포인트 하드코딩 검증이 막던 갭 회귀 가드.
        with mock.patch.object(routes_admin, "access_log_timeline",
                               return_value={"interval": "month", "buckets": []}), \
             mock.patch.object(routes_admin, "lineage_timeline",
                               return_value={"interval": "month", "buckets": []}), \
             mock.patch.object(routes_admin, "asset_timeline",
                               return_value={"interval": "month", "buckets": []}), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            for path in ("/admin/access-logs/timeline", "/admin/lineage/timeline",
                         "/admin/asset-timeline"):
                r = self.client.get(f"{path}?interval=month")
                self.assertEqual(r.status_code, 200, f"{path} interval=month 은 200 이어야 함")
                self.assertEqual(r.json()["interval"], "month")

    def test_timeline_group_by_action_multiseries(self):
        with mock.patch.object(routes_admin, "access_log_timeline",
                               return_value={"interval": "day", "group_by": "action",
                                             "series": [{"key": "search", "buckets": []}]}) as tl, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs/timeline?group_by=action")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["series"][0]["key"], "search")
        self.assertEqual(tl.call_args.kwargs["group_by"], "action")

    def test_timeline_bad_group_by_422(self):
        r = self.client.get("/admin/access-logs/timeline?group_by=evil")
        self.assertEqual(r.status_code, 422)

    def test_lineage_stats_endpoint_removed_404(self):
        # 055: GET /admin/lineage/stats 엔드포인트 제거(양쪽 프론트 미사용·함수는 dashboard 유지)
        r = self.client.get("/admin/lineage/stats")
        self.assertEqual(r.status_code, 404)
        # 우연한 404 아님을 보장 — 라우트 자체가 OpenAPI 에서 제거됐는지 확인
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertNotIn("/admin/lineage/stats", paths)
        # 회귀: 다른 stats 엔드포인트(대칭)는 유지
        self.assertIn("/admin/asset-stats", paths)
        self.assertIn("/admin/access-logs/stats", paths)

    def test_lineage_timeline_endpoint(self):
        with mock.patch.object(routes_admin, "lineage_timeline",
                               return_value={"interval": "day", "group_by": "activity",
                                             "series": [{"key": "ingest.registered.v1", "buckets": []}]}) as tl, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/lineage/timeline?group_by=activity")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["series"][0]["key"], "ingest.registered.v1")
        self.assertEqual(tl.call_args.kwargs["group_by"], "activity")

    def test_lineage_timeline_bad_group_by_422(self):
        r = self.client.get("/admin/lineage/timeline?group_by=evil")
        self.assertEqual(r.status_code, 422)

    def test_asset_stats_endpoint(self):
        with mock.patch.object(routes_admin, "asset_stats",
                               return_value={"total": 3, "by_status": [{"status": "registered", "count": 3}],
                                             "by_modality": [], "by_domain": [],
                                             "by_file_ext": [{"file_ext": "pdf", "count": 3}],
                                             "by_date": [{"date": "2026-06-30", "count": 3}]}), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/asset-stats")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["by_status"][0]["status"], "registered")
        # 신규 차원(file_ext·date)도 응답에 그대로 실린다(API 레벨).
        self.assertIn("by_file_ext", body)
        self.assertIn("by_date", body)

    def test_asset_stats_from_to_passthrough(self):
        # 기간별 파일 포맷 통계(프론트 ②) — from/to 가 asset_stats 로 전달되는지 배선 검증
        with mock.patch.object(routes_admin, "asset_stats",
                               return_value={"total": 0, "by_status": [], "by_modality": [],
                                             "by_domain": [], "by_file_ext": [], "by_date": []}) as st, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/asset-stats?from=2026-06-01&to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(st.call_args.kwargs["since"])
        self.assertIsNotNone(st.call_args.kwargs["until"])

    def test_asset_stats_bad_date_422(self):
        r = self.client.get("/admin/asset-stats?from=not-a-date")
        self.assertEqual(r.status_code, 422)

    def test_assets_list_endpoint(self):
        with mock.patch.object(routes_admin, "query_assets",
                               return_value={"rows": [{"asset_id": "a1", "status": "registered",
                                                       "modality": "text", "domain_label": "general",
                                                       "file_name": "x.txt", "created_at": "2026-06-30T00:00:00+00:00"}],
                                             "total": 1}), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets?status=registered&limit=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rows"][0]["status"], "registered")

    def test_assets_list_with_content_passthrough(self):
        # with_content=true 가 서비스로 전달되는지 배선 검증(보완 v6)
        with mock.patch.object(routes_admin, "query_assets",
                               return_value={"rows": [], "total": 0}) as qa, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets?modality=video&with_content=true")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(qa.call_args.kwargs["with_content"])
        self.assertEqual(qa.call_args.kwargs["modality"], "video")

    def test_modality_detail_endpoint(self):
        with mock.patch.object(routes_admin, "modality_detail",
                               return_value={"modality": "video", "total": 9,
                                             "by_file_ext": [{"file_ext": "mp4", "count": 7}],
                                             "by_status": [], "by_date": []}) as md, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/modality/video")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["modality"], "video")
        self.assertEqual(r.json()["by_file_ext"][0]["file_ext"], "mp4")
        self.assertEqual(md.call_args.args[1], "video")  # path param 전달

    def test_modality_detail_from_to_passthrough(self):
        with mock.patch.object(routes_admin, "modality_detail",
                               return_value={"modality": "video", "total": 0, "by_file_ext": [],
                                             "by_status": [], "by_date": []}) as md, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/modality/video?from=2026-06-01&to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(md.call_args.kwargs["since"])
        self.assertIsNotNone(md.call_args.kwargs["until"])

    def test_modality_detail_distinct_from_lineage_route(self):
        # /admin/assets/modality/{m} 가 /admin/assets/{id}/lineage 와 충돌하지 않음(구체 경로 우선)
        with mock.patch.object(routes_admin, "modality_detail",
                               return_value={"modality": "image", "total": 0, "by_file_ext": [],
                                             "by_status": [], "by_date": []}) as md, \
             mock.patch.object(routes_admin, "query_asset_lineage", return_value=[]) as ln, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/modality/image")
        self.assertEqual(r.status_code, 200)
        md.assert_called_once()
        ln.assert_not_called()  # 계보 핸들러로 새지 않음

    def test_asset_timeline_group_by_modality(self):
        with mock.patch.object(routes_admin, "asset_timeline",
                               return_value={"interval": "day", "group_by": "modality",
                                             "series": [{"key": "video", "buckets": []}]}) as tl, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/asset-timeline?group_by=modality")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["series"][0]["key"], "video")
        self.assertEqual(tl.call_args.kwargs["group_by"], "modality")

    def test_asset_timeline_group_by_file_ext(self):
        # 프론트 ③ 일별 파일 포맷 추이 — group_by=file_ext 허용(422 아님)·전달
        with mock.patch.object(routes_admin, "asset_timeline",
                               return_value={"interval": "day", "group_by": "file_ext",
                                             "series": [{"key": "pdf", "buckets": []}]}) as tl, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/asset-timeline?group_by=file_ext")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["series"][0]["key"], "pdf")
        self.assertEqual(tl.call_args.kwargs["group_by"], "file_ext")

    def test_asset_timeline_bad_interval_422(self):
        r = self.client.get("/admin/asset-timeline?interval=year")
        self.assertEqual(r.status_code, 422)

    def test_asset_timeline_bad_group_by_422(self):
        r = self.client.get("/admin/asset-timeline?group_by=evil")
        self.assertEqual(r.status_code, 422)

    # ── 057 FR-204: 관계 제안 distinct·추이 서버 집계(limit 캡 없음) ──────────────
    def test_relations_proposed_summary_endpoint(self):
        with mock.patch.object(routes_admin, "relation_proposed_summary",
                               return_value={"distinct_assets": 42,
                                             "timeline": {"interval": "day", "buckets": []}}) as s, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/relations/proposed-summary?from=2026-06-01&to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["distinct_assets"], 42)
        self.assertIn("timeline", r.json())
        self.assertIsNotNone(s.call_args.kwargs["since"])
        self.assertIsNotNone(s.call_args.kwargs["until"])

    def test_relations_proposed_summary_interval_passthrough(self):
        with mock.patch.object(routes_admin, "relation_proposed_summary",
                               return_value={"distinct_assets": 0,
                                             "timeline": {"interval": "month", "buckets": []}}) as s, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/relations/proposed-summary?interval=month")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(s.call_args.kwargs["interval"], "month")

    def test_relations_proposed_summary_bad_interval_422(self):
        r = self.client.get("/admin/relations/proposed-summary?interval=year")
        self.assertEqual(r.status_code, 422)

    def test_relations_proposed_summary_not_shadowed_by_relations_list(self):
        # /admin/relations/proposed-summary 가 /admin/relations(검토 큐)로 새지 않음(구체 경로 우선).
        with mock.patch.object(routes_admin, "relation_proposed_summary",
                               return_value={"distinct_assets": 0,
                                             "timeline": {"interval": "day", "buckets": []}}) as s, \
             mock.patch.object(routes_admin, "list_edges_for_review", return_value={"rows": []}) as lst, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/relations/proposed-summary")
        self.assertEqual(r.status_code, 200)
        s.assert_called_once()
        lst.assert_not_called()  # 검토 큐 핸들러로 새지 않음

    # ── 057 FR-301: access-logs overview BFF(stats+timeline 1회) ───────────────
    def test_access_logs_overview_endpoint(self):
        with mock.patch.object(routes_admin, "access_log_overview",
                               return_value={"total": 12, "by_action": [{"action": "search", "count": 12}],
                                             "timeline": {"interval": "day", "group_by": "action",
                                                          "series": []}}) as ov, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs/overview?from=2026-06-01&to=2026-06-30&action=search")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total"], 12)
        self.assertIn("by_action", body)
        self.assertIn("timeline", body)
        kw = ov.call_args.kwargs
        self.assertEqual(kw["action"], "search")
        self.assertIsNotNone(kw["since"])
        self.assertIsNotNone(kw["until"])

    def test_access_logs_overview_interval_passthrough(self):
        with mock.patch.object(routes_admin, "access_log_overview",
                               return_value={"total": 0, "by_action": [],
                                             "timeline": {"interval": "month", "series": []}}) as ov, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs/overview?interval=month")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ov.call_args.kwargs["interval"], "month")

    def test_access_logs_overview_bad_interval_422(self):
        r = self.client.get("/admin/access-logs/overview?interval=year")
        self.assertEqual(r.status_code, 422)

    def test_access_logs_overview_not_shadowed_by_list(self):
        # /admin/access-logs/overview 가 /admin/access-logs(목록)로 새지 않음(구체 경로 우선).
        with mock.patch.object(routes_admin, "access_log_overview",
                               return_value={"total": 0, "by_action": [],
                                             "timeline": {"interval": "day", "series": []}}) as ov, \
             mock.patch.object(routes_admin, "query_access_logs", return_value={"rows": [], "total": 0}) as lst, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/access-logs/overview")
        self.assertEqual(r.status_code, 200)
        ov.assert_called_once()
        lst.assert_not_called()

    # ── 057 FR-302: 모달리티 현황 BFF(detail+timeline+first-page 1회) ───────────
    def test_modality_overview_endpoint(self):
        with mock.patch.object(routes_admin, "build_modality_overview",
                               return_value={"detail": {"modality": "video", "total": 9},
                                             "timeline": {"interval": "month", "buckets": []},
                                             "first_page": {"rows": [], "total": 9}}) as ov, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get(
                "/admin/assets/modality/video/overview?from=2026-06-01&to=2026-06-30&interval=month&limit=10")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("detail", body)
        self.assertIn("timeline", body)
        self.assertIn("first_page", body)
        self.assertEqual(ov.call_args.args[1], "video")  # path param modality
        kw = ov.call_args.kwargs
        self.assertEqual(kw["interval"], "month")
        self.assertEqual(kw["limit"], 10)
        self.assertIsNotNone(kw["since"])

    def test_modality_overview_bad_interval_422(self):
        r = self.client.get("/admin/assets/modality/video/overview?interval=year")
        self.assertEqual(r.status_code, 422)

    def test_modality_overview_not_shadowed_by_asset_detail(self):
        # /admin/assets/modality/{m}/overview 가 /admin/assets/{id} catch-all 로 새지 않음.
        with mock.patch.object(routes_admin, "build_modality_overview",
                               return_value={"detail": {}, "timeline": {}, "first_page": {}}) as ov, \
             mock.patch.object(routes_admin, "fetch_asset_detail", return_value={"asset_id": "x"}) as det, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/modality/image/overview")
        self.assertEqual(r.status_code, 200)
        ov.assert_called_once()
        det.assert_not_called()

    def test_record_access_safe_records_data_route(self):
        # 기록 결정 로직 직접 검증(미들웨어 fire-and-forget 타이밍과 무관·결정적):
        # 데이터 라우트 성공 응답 → record_access(action=asset_view·asset_id) 1회.
        # 2026-07-15 B3: asset_id 세그먼트는 UUID 형식만 감사 대상(비-UUID 는 아래 skip 테스트).
        aid = "018f0000-0000-7000-8000-000000000252"
        with mock.patch.object(_infra, "_run_in_db_write", side_effect=lambda cb: cb(None)), \
             mock.patch.object(_infra, "record_access") as rec:
            _infra._record_access_safe("GET", f"/assets/{aid}", 200, "u1")
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["action"], "asset_view")
        self.assertEqual(rec.call_args.kwargs["asset_id"], aid)
        self.assertEqual(rec.call_args.kwargs["user_id"], "u1")

    def test_record_access_safe_skips_non_data_and_error_status(self):
        with mock.patch.object(_infra, "record_access") as rec:
            _infra._record_access_safe("GET", "/health", 200, "u1")     # 비대상 라우트
            _infra._record_access_safe("GET", "/assets/a1", 404, "u1")  # 4xx
            _infra._record_access_safe("GET", "/admin/access-logs", 200, "u1")  # 감사 뷰(자기 기록 안 함)
            # 신규 관리자/대시보드 뷰(/admin/*)도 자기 기록 안 함(노이즈 방지·통합 경로 검증).
            _infra._record_access_safe("GET", "/admin/lineage", 200, "u1")
            _infra._record_access_safe("GET", "/admin/access-logs/timeline", 200, "u1")
            _infra._record_access_safe("GET", "/admin/asset-stats", 200, "u1")
            _infra._record_access_safe("GET", "/admin/assets", 200, "u1")
            # 보완 v6 신규 관리자 뷰도 자기 기록 안 함(/admin/* 는 데이터 라우트 아님)
            _infra._record_access_safe("GET", "/admin/assets/modality/video", 200, "u1")
            _infra._record_access_safe("GET", "/admin/asset-timeline", 200, "u1")
        rec.assert_not_called()

    def test_middleware_schedules_recording_non_blocking(self):
        # 미들웨어는 기록을 await 하지 않고 create_task 로 스케줄(비차단)·응답은 그대로 반환.
        # _record_access_bg 를 AsyncMock 으로 가로채 호출 인자만 확인(실 DB·실제 태스크 실행 불요).
        # 표본은 실제 UUID — B3(비-UUID 세그먼트 감사 제외) 이후 "a1" 류는 유효 asset_id 표본이 아니다.
        aid = "018f0000-0000-7000-8000-000000000252"
        with mock.patch.object(routes_assets, "fetch_asset_detail", return_value={"asset_id": aid}), \
             mock.patch.object(routes_assets, "fetch_asset_topic", return_value=[]), \
             mock.patch.object(routes_assets, "find_same_topic_groups", return_value=[]), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)), \
             mock.patch.object(_infra, "_record_access_bg", new=mock.AsyncMock()) as bg:
            r = self.client.get(f"/assets/{aid}")
        self.assertEqual(r.status_code, 200)          # 응답 정상(기록과 분리)
        bg.assert_called_once()                       # 기록은 스케줄됨
        self.assertEqual(bg.call_args.args[0], "GET")
        self.assertEqual(bg.call_args.args[1], f"/assets/{aid}")


class SnapshotBucketApiTest(unittest.TestCase):
    """054 G3 — /admin/assets snapshot_bucket·relation_scope·/admin/asset-stats snapshot_buckets·
    /admin/assets/{id} 배선(FR-103/201/301). 전부 additive·기존 동작 불변."""

    def setUp(self):
        self.client = TestClient(app)

    def test_assets_list_snapshot_bucket_passthrough(self):
        # snapshot_bucket·relation_scope 가 query_assets 로 전달되는지 배선 검증(FR-103).
        with mock.patch.object(routes_admin, "query_assets",
                               return_value={"rows": [], "total": 0}) as qa, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get(
                "/admin/assets?snapshot_bucket=relation_proposed"
                "&created_from=2026-06-01&created_to=2026-06-30")
        self.assertEqual(r.status_code, 200)
        kw = qa.call_args.kwargs
        self.assertEqual(kw["snapshot_bucket"], "relation_proposed")
        self.assertEqual(kw["relation_scope"], "period")  # 기본값
        self.assertIsNotNone(kw["created_from"])
        self.assertIsNotNone(kw["created_to"])

    def test_assets_list_relation_scope_alltime_passthrough(self):
        with mock.patch.object(routes_admin, "query_assets",
                               return_value={"rows": [], "total": 0}) as qa, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get(
                "/admin/assets?snapshot_bucket=registered&relation_scope=alltime")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(qa.call_args.kwargs["relation_scope"], "alltime")

    def test_assets_list_bad_snapshot_bucket_400(self):
        # 화이트리스트(_SNAPSHOT_BUCKETS) 밖 버킷은 400(query_assets 호출 없음).
        with mock.patch.object(routes_admin, "query_assets") as qa, \
             mock.patch.object(_infra, "_run_in_db",
                               side_effect=lambda cb: cb(None)):  # 400 조기반환 시 미호출
            r = self.client.get("/admin/assets?snapshot_bucket=xxx")
        self.assertEqual(r.status_code, 400)
        qa.assert_not_called()

    def test_assets_list_bad_relation_scope_400(self):
        with mock.patch.object(routes_admin, "query_assets") as qa, \
             mock.patch.object(_infra, "_run_in_db",
                               side_effect=lambda cb: cb(None)):  # 400 조기반환 시 미호출
            r = self.client.get("/admin/assets?relation_scope=xxx")
        self.assertEqual(r.status_code, 400)
        qa.assert_not_called()

    def test_assets_list_no_snapshot_bucket_unchanged(self):
        # 미지정 시 기존 동작 불변(하위호환) — snapshot_bucket=None·relation_scope 기본.
        with mock.patch.object(routes_admin, "query_assets",
                               return_value={"rows": [], "total": 0}) as qa, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets?status=registered")
        self.assertEqual(r.status_code, 200)
        kw = qa.call_args.kwargs
        self.assertIsNone(kw["snapshot_bucket"])
        self.assertEqual(kw["status"], "registered")

    def test_asset_stats_snapshot_buckets_flag_passthrough(self):
        # snapshot_buckets=1 → asset_stats(snapshot_buckets=True)(FR-201).
        with mock.patch.object(
                routes_admin, "asset_stats",
                return_value={"total": 0, "by_status": [], "by_modality": [], "by_domain": [],
                              "by_file_ext": [], "by_date": [],
                              "by_snapshot_bucket": [{"bucket": "processing", "count": 0}]}) as st, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/asset-stats?snapshot_buckets=1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(st.call_args.kwargs["snapshot_buckets"])
        self.assertIn("by_snapshot_bucket", r.json())

    def test_asset_stats_snapshot_buckets_default_false(self):
        # 미지정 시 snapshot_buckets=False(하위호환·기존 응답만).
        with mock.patch.object(
                routes_admin, "asset_stats",
                return_value={"total": 0, "by_status": [], "by_modality": [], "by_domain": [],
                              "by_file_ext": [], "by_date": []}) as st, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/asset-stats")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(st.call_args.kwargs["snapshot_buckets"])

    def test_asset_detail_endpoint(self):
        # /admin/assets/{id} → fetch_asset_detail 호출·정상 detail 200.
        with mock.patch.object(routes_admin, "fetch_asset_detail",
                               return_value={"asset_id": "a1", "modality": "text",
                                             "status": "registered"}) as fd, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/a1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["asset_id"], "a1")
        self.assertEqual(fd.call_args.kwargs["asset_id"], "a1")

    def test_asset_detail_none_404(self):
        # 없음/의료/비registered → fetch_asset_detail None → 404.
        with mock.patch.object(routes_admin, "fetch_asset_detail", return_value=None), \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/nope")
        self.assertEqual(r.status_code, 404)

    def test_asset_detail_requires_auth_401(self):
        # require_principal — 인증 없으면 401(auth 활성·토큰 없음). 관계 테스트 관례 재사용.
        from service.portal.auth.verifier import _reset_verifier_for_tests

        _reset_verifier_for_tests()
        with mock.patch.dict(os.environ,
                             {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "test-secret"},
                             clear=False):
            client = TestClient(app)
            r = client.get("/admin/assets/a1")
        _reset_verifier_for_tests()
        self.assertEqual(r.status_code, 401)

    def test_asset_detail_does_not_shadow_modality_route(self):
        # 라우트 순서 회귀: /admin/assets/modality/{m} 는 여전히 modality_detail 로 매칭
        # (신설 /admin/assets/{id} 로 새지 않음·C8).
        with mock.patch.object(routes_admin, "modality_detail",
                               return_value={"modality": "text", "total": 0, "by_file_ext": [],
                                             "by_status": [], "by_date": []}) as md, \
             mock.patch.object(routes_admin, "fetch_asset_detail") as fd, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/modality/text")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["modality"], "text")
        md.assert_called_once()
        fd.assert_not_called()  # 자산 상세 핸들러로 새지 않음

    def test_asset_detail_does_not_shadow_lineage_route(self):
        # 라우트 순서 회귀: /admin/assets/{id}/lineage 는 여전히 query_asset_lineage 로 매칭.
        with mock.patch.object(routes_admin, "query_asset_lineage", return_value=[]) as ln, \
             mock.patch.object(routes_admin, "fetch_asset_detail") as fd, \
             mock.patch.object(_infra, "_run_in_db", side_effect=lambda cb: cb(None)):
            r = self.client.get("/admin/assets/a1/lineage")
        self.assertEqual(r.status_code, 200)
        self.assertIn("activities", r.json())
        ln.assert_called_once()
        fd.assert_not_called()  # 자산 상세 핸들러로 새지 않음


if __name__ == "__main__":
    unittest.main()
