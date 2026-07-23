"""포탈 FastAPI 진입점(`src/app/portal_api.py`) 단위 테스트 — DB·LLM 불필요.

전략(plan 010 D-7, G4)
    FastAPI ``TestClient`` 로 라우팅·상태코드·계약·의료배제만 검증한다. 소비 서비스 함수
    (``search_hybrid``/``fetch_asset_detail``/``resolve_download_target``/
    ``collect_bundle_assets``/``build_bundle_zip``)와 DB 실행 seam(``_run_in_db``)을
    ``unittest.mock.patch`` 로 대체해 **DB·LLM·네트워크 없이** 순수 단위로 돈다.

검증 대상
    - T022: ``/health`` 200 · ``/search`` 정상(query+results(모달리티별)+meta) · 버킷별 의료
      배제(FR-014) · size top-N.
    - T023: ``/assets/{id}`` 200/404 · ``/assets/{id}/download`` Range→206+Content-Range·
      원본 없음→404/410(FR-009) · ``/assets/{id}/bundle`` → application/zip · seed 게이트 404.

주의: ``TestClient(app)`` 를 ``with`` 없이 쓰면 lifespan(init_settings)이 돌지 않으므로
``.env``·DB 없이 라우팅만 검증된다(부트스트랩은 G5 실DB e2e 책임).
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app


def _passthrough_db(callback):
    """``_run_in_db`` 대역: 가짜 conn 으로 callback 을 즉시 실행한다(DB 불필요).

    실제 DB 조회 함수들은 각 테스트에서 patch 로 대체되므로, 넘기는 conn 값은 무의미하다.
    """
    return callback(object())


def _empty_tiers(*_args, **_kwargs):
    """registry 미조회 단위 테스트 — tier 미등록 키는 projection 통과."""
    return {}


_AUTH_DISABLED_ENV = {
    "PORTAL_AUTH_DISABLED": "1",
    "PORTAL_JWT_SECRET": "test-secret",
}


def _enable_portal_test_auth_bypass(test_case: unittest.TestCase) -> None:
    """보호 라우트 단위 테스트용 dev bypass + DB/tier mock."""
    env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
    env.start()
    test_case.addCleanup(env.stop)
    db = patch("service.api._infra._run_in_db", _passthrough_db)
    db.start()
    test_case.addCleanup(db.stop)


def _fake_search_result() -> dict:
    """``search_hybrid`` 가 돌려주는 모달리티 버킷 결과 대역.

    의료 행(domain_label='medical')을 image 버킷에 섞어 FR-014 버킷별 배제를 검증할 수 있게
    한다. text 는 a1>a2>a3, image 는 의료(0.95) 1건뿐이라 배제 후 빈 섹션이 된다.
    """
    return {
        "query": "회식",
        "results": {
            "text_documents": [
                {"id": "a1", "similarity": 0.9, "file_uri": "/x/a1.txt", "summary": "s1"},
                {"id": "a2", "similarity": 0.8, "file_uri": "/x/a2.txt", "summary": "s2"},
                {"id": "a3", "similarity": 0.7, "file_uri": "/x/a3.txt", "summary": "s3"},
            ],
            "image": [
                {
                    "id": "med1",
                    "similarity": 0.95,
                    "file_uri": "/x/m.png",
                    "summary": "medical",
                    "domain_label": "medical",
                },
            ],
        },
        "meta": {"fusion": "alpha"},
    }


class TestHealth(unittest.TestCase):
    """``/health`` 헬스 체크."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_returns_ok(self) -> None:
        # 설정 초기화 없이도 헬스는 200 + 환경 라벨을 돌려준다.
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["env"], os.getenv("PORTAL_API_ENV", "dev"))


class TestSearch(unittest.TestCase):
    """``/search`` — 모달리티별 그룹 응답·버킷별 의료배제·size top-N."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        tiers = patch("service.api.routes_search.fetch_access_tiers", side_effect=_empty_tiers)
        tiers.start()
        self.addCleanup(tiers.stop)
        # 057-후속/065: /search 주제 패싯(FR-503)은 결과 행의 **색인 topics**(=필터 소스)로 계산하며
        # 별도 DB 주제 seam 을 호출하지 않는다(라이브 투영 미사용). 패싯 자체 검증은 test_portal_topics.
        self.client = TestClient(app)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_returns_grouped_contract(self, mock_search) -> None:
        # 정상 검색: query + results(모달리티별 dict) + meta(counts). cursor/평탄 items 없음.
        mock_search.return_value = _fake_search_result()
        resp = self.client.get("/search", params={"q": "회식", "size": 10})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn("next_cursor", body)  # cursor 제거됨
        self.assertEqual(body["query"], "회식")
        # results 는 모달리티별 dict — text 섹션 안에서만 랭킹(a1>a2>a3).
        self.assertEqual([r["asset_id"] for r in body["results"]["text"]], ["a1", "a2", "a3"])
        # 2026-07-23: 도메인 제외 전면 제거 — image 섹션의 의료 자산(med1)도 노출된다.
        self.assertEqual([r["asset_id"] for r in body["results"]["image"]], ["med1"])
        self.assertEqual(body["meta"]["counts"], {"text": 3, "image": 1})
        self.assertEqual(body["meta"]["size"], 10)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_meta_propagates_observability_when_present(self, mock_search) -> None:
        # 069 P1-4: search_hybrid meta 의 관측성 3종(os_gate·llm_verify·query_norm)을 포탈이 전파.
        r = _fake_search_result()
        r["meta"].update({
            "os_gate": {"text": {"gate_passed": True}},
            "llm_verify": {"verified": 3, "dropped": 1, "fallback": False},
            "query_norm": {"enabled": True, "method": "morph", "original": "회식 영상", "normalized": "회식"},
        })
        mock_search.return_value = r
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        self.assertEqual(body["meta"]["os_gate"], {"text": {"gate_passed": True}})
        self.assertEqual(body["meta"]["llm_verify"]["dropped"], 1)
        self.assertEqual(body["meta"]["query_norm"]["method"], "morph")

    @patch("service.api.routes_search.search_hybrid")
    def test_search_meta_observability_keys_absent_when_off(self, mock_search) -> None:
        # off 관례: search_hybrid meta 에 없으면 포탈 meta 에도 키 부재(빈 값 주입 금지).
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        for k in ("os_gate", "llm_verify", "query_norm"):
            self.assertNotIn(k, body["meta"])

    @patch("service.api.routes_search.search_hybrid")
    def test_os_connection_error_returns_503(self, mock_search) -> None:
        # 069 P1-4 권고: OS 연결 실패(인프라)는 503 — 코드버그 500 과 구분(운영 알람 분리).
        from opensearchpy.exceptions import ConnectionError as OSConnectionError

        mock_search.side_effect = OSConnectionError("N/A", "conn refused", None)
        resp = self.client.get("/search", params={"q": "회식"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("OpenSearch", resp.json()["detail"])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_limit_per_bucket_param_and_default(self, mock_search) -> None:
        # 후보 풀(limit_per_bucket) 요청 파라미터화: 미지정=기본 50, 지정 시 그 값이 search_hybrid 에 전달.
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "size": 10})
        self.assertEqual(mock_search.call_args.kwargs["limit_per_bucket"], 50)  # 기본값
        self.client.get("/search", params={"q": "회식", "size": 10, "limit_per_bucket": 200})
        self.assertEqual(mock_search.call_args.kwargs["limit_per_bucket"], 200)  # 요청 지정

    @patch("service.api.routes_search.search_hybrid")
    def test_search_pool_floored_to_size(self, mock_search) -> None:
        # size 계약 보장: 요청 풀이 size 보다 얕으면 max(풀, size) 로 끌어올린다(풀<size 회귀 방지).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "size": 80, "limit_per_bucket": 20})
        self.assertEqual(mock_search.call_args.kwargs["limit_per_bucket"], 80)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_limit_per_bucket_bounds(self, mock_search) -> None:
        # 상한(500) 초과·하한(1) 미만은 422(Query ge/le 계약).
        mock_search.return_value = _fake_search_result()
        self.assertEqual(self.client.get("/search", params={"q": "x", "limit_per_bucket": 501}).status_code, 422)
        self.assertEqual(self.client.get("/search", params={"q": "x", "limit_per_bucket": 0}).status_code, 422)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_response_rows_include_topic_pairs(self, mock_search) -> None:
        # 059 FR-104: /search 응답 행에 topic_pairs(부모>자식 짝) 포함(하위호환 필드·프론트 트리용).
        # 짝 없는 행은 [] 폴백. os_hit_to_row→_shape→_project_grouped_search 경유로 전달된다.
        mock_search.return_value = {
            "query": "먹방",
            "results": {
                "text_documents": [
                    {
                        "id": "a1",
                        "similarity": 0.9,
                        "file_uri": "/x/a1.mp4",
                        "summary": "s1",
                        "topics": ["음식·요리", "IT·기술"],
                        "subtopics": ["먹방", "데이터"],
                        "topic_pairs": ["음식·요리>먹방", "IT·기술>데이터"],
                    },
                    {"id": "a2", "similarity": 0.8, "file_uri": "/x/a2.txt", "summary": "s2"},
                ],
            },
            "meta": {},
        }
        body = self.client.get("/search", params={"q": "먹방", "size": 10}).json()
        rows = body["results"]["text"]
        self.assertEqual(rows[0]["topic_pairs"], ["음식·요리>먹방", "IT·기술>데이터"])
        self.assertEqual(rows[1]["topic_pairs"], [])  # 짝 없는 행 → [] 폴백(하위호환)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_includes_medical_per_bucket(self, mock_search) -> None:
        # 2026-07-23: 도메인 제외 전면 제거 — 의료 자산(med1)도 해당 버킷에 노출된다.
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        all_ids = [r["asset_id"] for rows in body["results"].values() for r in rows]
        self.assertIn("med1", all_ids)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_passes_exclude_and_size_to_group(self, mock_search) -> None:
        # 배선: group_ranked 가 exclude_domains(2026-07-23 빈집합) · limit_per_modality=size 로 호출되는지.
        mock_search.return_value = _fake_search_result()
        with patch("service.api.routes_search.group_ranked", return_value={}) as mock_group:
            self.client.get("/search", params={"q": "x", "size": 7})
        self.assertEqual(
            mock_group.call_args.kwargs["exclude_domains"], frozenset()
        )
        self.assertEqual(mock_group.call_args.kwargs["limit_per_modality"], 7)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_size_caps_per_modality(self, mock_search) -> None:
        # size=2 → 각 모달리티 섹션이 상위 2건으로 제한된다(섹션별 독립 top-N).
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 2}).json()
        self.assertEqual([r["asset_id"] for r in body["results"]["text"]], ["a1", "a2"])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_passes_mode_and_exposes_search_plan(self, mock_search) -> None:
        mock_search.return_value = {
            **_fake_search_result(),
            "meta": {
                "search_plan": {
                    "content_query": "테스트",
                    "lexical_rescue": "restricted",
                    "generic_single_term": True,
                    "mode": "auto",
                    "suggestions": ["hint"],
                },
            },
        }
        resp = self.client.get("/search", params={"q": "테스트", "mode": "auto"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_search.call_args.kwargs["search_mode"], "auto")
        plan = resp.json()["meta"]["search_plan"]
        self.assertEqual(plan["lexical_rescue"], "restricted")
        self.assertTrue(plan["generic_single_term"])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_invalid_mode_400(self, mock_search) -> None:
        resp = self.client.get("/search", params={"q": "x", "mode": "invalid"})
        self.assertEqual(resp.status_code, 400)
        mock_search.assert_not_called()

    @patch("service.api.routes_search.search_hybrid")
    def test_search_unknown_modality_400(self, mock_search) -> None:
        # 069 T301: 포탈은 미지 모달리티를 현행대로 HTTPException 400 으로 거부(공유 상수 검증·계약 보존).
        resp = self.client.get("/search", params={"q": "x", "modalities": "bogus"})
        self.assertEqual(resp.status_code, 400)
        mock_search.assert_not_called()

    @patch("service.api.routes_search.search_hybrid")
    def test_search_valid_modalities_passthrough(self, mock_search) -> None:
        # 069 T301: 유효 모달리티(공유 파서)는 그대로 search_hybrid 로 전달(valid 입력 결과 불변).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "modalities": "text,image"})
        self.assertEqual(mock_search.call_args.kwargs["modalities"], ["text", "image"])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_passes_v1_filters(self, mock_search) -> None:
        mock_search.return_value = _fake_search_result()
        resp = self.client.get(
            "/search",
            params=[
                ("q", "회식"),
                ("file_ext", "txt"),
                ("file_ext", "pdf"),
                ("source_dataset", "wikipedia"),
                ("created_from", "2026-01-01"),
                ("created_to", "2026-06-30"),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        sf = mock_search.call_args.kwargs["search_filters"]
        self.assertEqual(sf.file_exts, ("pdf", "txt"))
        self.assertEqual(sf.source_datasets, ("wikipedia",))
        meta_filters = resp.json()["meta"]["filters"]
        self.assertEqual(meta_filters["file_ext"], ["pdf", "txt"])
        self.assertEqual(meta_filters["source_dataset"], ["wikipedia"])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_passes_must_include_exclude(self, mock_search) -> None:
        # 057 FR-202: 반복 쿼리 파라미터 must_include/must_exclude 를 search_hybrid 에 배선한다.
        mock_search.return_value = _fake_search_result()
        resp = self.client.get(
            "/search",
            params=[
                ("q", "충전"),
                ("must_include", "배터리"),
                ("must_include", "고속"),
                ("must_exclude", "광고"),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_search.call_args.kwargs["must_include"], ["배터리", "고속"])
        self.assertEqual(mock_search.call_args.kwargs["must_exclude"], ["광고"])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_no_lexical_filters_forwards_empty(self, mock_search) -> None:
        # 미지정이면 빈 리스트로 전달(하위호환 — OS 본문 무변경).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "충전", "size": 5})
        self.assertEqual(mock_search.call_args.kwargs["must_include"], [])
        self.assertEqual(mock_search.call_args.kwargs["must_exclude"], [])

    @patch("service.api.routes_search.search_hybrid")
    def test_search_invalid_date_returns_422(self, mock_search) -> None:
        resp = self.client.get(
            "/search",
            params=[("q", "테스트"), ("created_from", "not-a-date")],
        )
        self.assertEqual(resp.status_code, 422)
        mock_search.assert_not_called()

    # ── 069 T407: sample_search_api 디버그 3종을 portal /search opt-in 으로 이관 ──────
    # 전부 기본 off = 기존 응답 불변. no_cutoff 는 search_hybrid 배선, compact/group_by_relation 은
    # 이미 group_ranked 로 의료 배제·projection 된 grouped 결과 위에서 축약/묶음 뷰를 만든다.

    @patch("service.api.routes_search.search_hybrid")
    def test_no_cutoff_default_off_not_disabled(self, mock_search) -> None:
        # 기본(off): disable_os_cutoff=True 를 전달하지 않는다(동작 불변).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식"})
        self.assertNotEqual(mock_search.call_args.kwargs.get("disable_os_cutoff"), True)

    @patch("service.api.routes_search.search_hybrid")
    def test_no_cutoff_true_wires_disable_os_cutoff(self, mock_search) -> None:
        # no_cutoff=true → search_hybrid(disable_os_cutoff=True) 배선(027 디버그 우회).
        mock_search.return_value = _fake_search_result()
        self.client.get("/search", params={"q": "회식", "no_cutoff": "true"})
        self.assertIs(mock_search.call_args.kwargs["disable_os_cutoff"], True)

    @patch("service.api.routes_search.search_hybrid")
    def test_compact_default_off_returns_grouped(self, mock_search) -> None:
        # 기본(off): 기존 grouped 응답 계약 불변.
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "size": 10}).json()
        self.assertIn("results", body)
        self.assertNotIn("결과", body)

    @patch("service.api.routes_search.search_hybrid")
    def test_compact_true_returns_flat_ranking(self, mock_search) -> None:
        # compact=true → {query, 건수, 결과} 축약 뷰(전 모달리티 합쳐 점수순). 2026-07-23: 도메인 제외 전면 제거.
        mock_search.return_value = _fake_search_result()
        body = self.client.get("/search", params={"q": "회식", "compact": "true"}).json()
        self.assertEqual(body["query"], "회식")
        self.assertIn("건수", body)
        self.assertIn("결과", body)
        # 의료(image med1·0.95)도 이제 포함 → 4건, 점수순 med1>a1>a2>a3.
        self.assertEqual(body["건수"], 4)
        rows = body["결과"]
        self.assertEqual([r["순위"] for r in rows], [1, 2, 3, 4])
        # 점수 내림차순(med1 0.95 > a1 0.9 > a2 0.8 > a3 0.7), 각 행에 모달리티·점수·파일명·요약.
        self.assertEqual([r["파일명"] for r in rows], ["m.png", "a1.txt", "a2.txt", "a3.txt"])
        self.assertEqual([r["모달리티"] for r in rows], ["image", "text", "text", "text"])
        for r in rows:
            self.assertIn("점수", r)
            self.assertIn("요약", r)
        # 의료 자산도 이제 축약 뷰에 노출된다(도메인 제외 없음).
        self.assertIn("m.png", [r["파일명"] for r in rows])

    @patch("service.api.routes_search.fetch_active_relations_for_asset")
    @patch("service.api.routes_search.search_hybrid")
    def test_group_by_relation_folds_same_source(self, mock_search, mock_edges) -> None:
        # group_by_relation=true → 같은 소스 엣지(active duplicate_near/derived_from)로 묶음.
        # a1(text)·v1(video)이 duplicate_near 로 이어져 한 묶음, a2 는 별도 묶음.
        result = {
            "query": "회식",
            "results": {
                "text_documents": [
                    {"id": "a1", "similarity": 0.9, "file_uri": "/x/a1.txt", "summary": "s1"},
                    {"id": "a2", "similarity": 0.6, "file_uri": "/x/a2.txt", "summary": "s2"},
                ],
                "video": [
                    {"id": "v1", "similarity": 0.8, "file_uri": "/x/회식.mp4", "summary": "vs"},
                ],
            },
            "meta": {},
        }
        mock_search.return_value = result

        def _neighbors(_conn, *, asset_id, status="active"):
            # a1↔v1 은 same-source(duplicate_near·대칭 엣지 양방향), 그 외 이웃 없음.
            if asset_id == "a1":
                return [{"asset_id": "v1", "kind_code": "duplicate_near"}]
            if asset_id == "v1":
                return [{"asset_id": "a1", "kind_code": "duplicate_near"}]
            return []

        mock_edges.side_effect = _neighbors
        body = self.client.get(
            "/search", params={"q": "회식", "group_by_relation": "true"}
        ).json()
        self.assertEqual(body["query"], "회식")
        self.assertIn("묶음", body)
        self.assertEqual(body["묶음수"], 2)  # {a1,v1} + {a2}
        # graph_query seam(대칭 양방향)을 경유했는지 — 순진한 단방향 SQL 우회 금지(CR-19).
        self.assertTrue(mock_edges.called)
        # 묶음점수 내림차순: {a1(0.9),v1(0.8)} 대표 0.9 먼저, {a2 0.6} 뒤.
        first = body["묶음"][0]
        self.assertEqual(first["묶음점수"], 0.9)
        self.assertEqual({m["파일명"] for m in first["구성"]}, {"a1.txt", "회식.mp4"})
        second = body["묶음"][1]
        self.assertEqual([m["파일명"] for m in second["구성"]], ["a2.txt"])

    @patch("service.api.routes_search.fetch_active_relations_for_asset")
    @patch("service.api.routes_search.search_hybrid")
    def test_group_by_relation_priority_over_compact(self, mock_search, mock_edges) -> None:
        # 조합 우선순위(sample 보존): group_by_relation 이 compact 보다 우선.
        mock_search.return_value = _fake_search_result()
        mock_edges.return_value = []
        body = self.client.get(
            "/search",
            params={"q": "회식", "compact": "true", "group_by_relation": "true"},
        ).json()
        self.assertIn("묶음", body)   # group 뷰
        self.assertNotIn("결과", body)  # compact 뷰 아님

    @patch("service.api.routes_search.fetch_active_relations_for_asset")
    @patch("service.api.routes_search.search_hybrid")
    def test_group_by_relation_excludes_manifest_hub(self, mock_search, mock_edges) -> None:
        # manifest.json 허브는 묶음에서 제외(sample 보존).
        result = {
            "query": "q",
            "results": {
                "text_documents": [
                    {"id": "a1", "similarity": 0.9, "file_uri": "/x/a1.txt", "summary": "s1"},
                    {"id": "h1", "similarity": 0.5, "file_uri": "/x/manifest.json", "summary": ""},
                ],
            },
            "meta": {},
        }
        mock_search.return_value = result
        mock_edges.return_value = []
        body = self.client.get("/search", params={"q": "q", "group_by_relation": "true"}).json()
        self.assertEqual(body["묶음수"], 1)  # a1 만(manifest 제외)


class TestAssetDetail(unittest.TestCase):
    """``/assets/{id}`` — 상세 200 / 노출 게이트 404."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        # 065: 자산상세는 노출 통과 시 topics·same_topic_groups 를 같은 트랜잭션에서 계산하며
        # fetch_asset_topic/find_same_topic_groups(자기주제 정본 seam)를 호출한다. object() conn 단위
        # 테스트에선 fetch_asset_detail 과 동일하게 이 seam 들을 스텁한다(보강 검증은 test_portal_topics).
        for name in ("fetch_asset_topic", "find_same_topic_groups"):
            p = patch(f"service.api.routes_assets.{name}", return_value=[])
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)

    @patch("service.api.routes_assets.fetch_asset_detail")
    def test_detail_returns_200(self, mock_detail) -> None:
        detail = {
            "asset_id": "a1",
            "modality": "text",
            "domain_label": "general",
            "status": "registered",
            "core_meta": {"k": "v"},
            "ext_meta": {"summary": "요약"},
            "tags": [],
            "embedding_channels": [{"channel": "st", "chunk_count": 3}],
            "relations": [],
        }
        mock_detail.return_value = detail
        resp = self.client.get("/assets/a1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["asset_id"], "a1")
        self.assertEqual(resp.json()["embedding_channels"][0]["chunk_count"], 3)

    @patch("service.api.routes_assets.fetch_asset_detail")
    def test_detail_none_returns_404(self, mock_detail) -> None:
        # 없음/비registered/의료(FR-014) → fetch_asset_detail None → 404.
        mock_detail.return_value = None
        resp = self.client.get("/assets/nope")
        self.assertEqual(resp.status_code, 404)


class TestDownload(unittest.TestCase):
    """``/assets/{id}/download`` — 전체/Range/누락/게이트."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        self.client = TestClient(app)
        # 알려진 10바이트 임시 원본 — Range 바이트 무결성 검증용.
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        self.tmp.write(b"0123456789")
        self.tmp.close()
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.unlink(self.tmp.name))

    def _target(self, fs_path: str) -> dict:
        return {
            "asset_id": "a1",
            "fs_path": fs_path,
            "fs_uri": f"file://{fs_path}",
            "file_size": 10,
            "modality": "text",
            "file_name": "sample.txt",
        }

    @patch("service.api.routes_assets.resolve_download_target")
    def test_download_full_returns_200(self, mock_resolve) -> None:
        mock_resolve.return_value = self._target(self.tmp.name)
        resp = self.client.get("/assets/a1/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"0123456789")
        self.assertEqual(resp.headers["accept-ranges"], "bytes")
        self.assertIn("sample.txt", resp.headers["content-disposition"])

    @patch("service.api.routes_assets.resolve_download_target")
    def test_download_range_returns_206(self, mock_resolve) -> None:
        # Range 부분 요청 → 206 + Content-Range + 정확한 바이트 구간(SC-004 단위 근사).
        mock_resolve.return_value = self._target(self.tmp.name)
        resp = self.client.get(
            "/assets/a1/download", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(resp.content, b"2345")
        self.assertEqual(resp.headers["accept-ranges"], "bytes")

    @patch("service.api.routes_assets.resolve_download_target")
    def test_download_range_unsatisfiable_returns_416(self, mock_resolve) -> None:
        # 파일 크기 초과 범위 → parse_range_header ValueError → 416.
        mock_resolve.return_value = self._target(self.tmp.name)
        resp = self.client.get(
            "/assets/a1/download", headers={"Range": "bytes=100-200"}
        )
        self.assertEqual(resp.status_code, 416)

    @patch("service.api.routes_assets.resolve_download_target")
    def test_download_missing_file_returns_404_or_410(self, mock_resolve) -> None:
        # FR-009: DB 엔 있으나 원본 파일이 사라짐 → 자산 노출 없이 404/410.
        mock_resolve.return_value = self._target("/no/such/file/at/all.txt")
        resp = self.client.get("/assets/a1/download")
        self.assertIn(resp.status_code, (404, 410))

    @patch("service.api.routes_assets.resolve_download_target")
    def test_download_none_returns_404(self, mock_resolve) -> None:
        # 비registered/의료/없음 게이트 → None → 404.
        mock_resolve.return_value = None
        resp = self.client.get("/assets/x/download")
        self.assertEqual(resp.status_code, 404)


class TestBundle(unittest.TestCase):
    """``/assets/{id}/bundle`` — zip 응답 / seed 게이트 404."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        self.client = TestClient(app)

    @staticmethod
    def _mk_stream(targets):
        s = io.BytesIO(b"PK\x03\x04zipbytes")
        TestBundle._last_stream = s
        return s

    @patch("service.api.routes_assets.build_bundle_zip_stream", side_effect=_mk_stream.__func__)
    @patch("service.api.routes_assets.collect_bundle_assets")
    @patch("service.api.routes_assets.resolve_download_target")
    def test_bundle_returns_zip(self, mock_resolve, mock_collect, mock_zip) -> None:
        # seed 가 게이트(registered·비의료) 통과 → ego-network zip 스트리밍(069 P1-2: StreamingResponse).
        mock_resolve.return_value = {"asset_id": "seed", "fs_path": "/x/seed.txt"}
        mock_collect.return_value = [
            {"asset_id": "seed", "fs_path": "/x/seed.txt", "file_name": "seed.txt"}
        ]
        resp = self.client.get("/assets/seed/bundle")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "application/zip")
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertEqual(resp.content, b"PK\x03\x04zipbytes")
        mock_collect.assert_called_once()
        # 리뷰 🟡2 회귀: 응답 송신 후 BackgroundTask 가 스트림을 명시 close(FD 정리 — GC 의존 금지).
        self.assertTrue(TestBundle._last_stream.closed)

    @patch("service.api.routes_assets.collect_bundle_assets")
    @patch("service.api.routes_assets.resolve_download_target")
    def test_bundle_seed_gated_returns_404(self, mock_resolve, mock_collect) -> None:
        # 의료/비registered/없는 seed → resolve None → 404, collect 미호출.
        mock_resolve.return_value = None
        resp = self.client.get("/assets/medseed/bundle")
        self.assertEqual(resp.status_code, 404)
        mock_collect.assert_not_called()


class TestPortalAuth(unittest.TestCase):
    """042 JWT · /me · 보호 라우트 401."""

    def setUp(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        _reset_verifier_for_tests()
        self._env = patch.dict(
            os.environ,
            {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": "test-secret"},
            clear=False,
        )
        self._env.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        self._env.stop()
        _reset_verifier_for_tests()

    def test_search_without_token_returns_401(self) -> None:
        resp = self.client.get("/search", params={"q": "x"})
        self.assertEqual(resp.status_code, 401)

    def test_auth_token_disabled_when_auth_enabled(self) -> None:
        resp = self.client.post("/auth/token", json={"username": "alice"})
        self.assertEqual(resp.status_code, 404)

    def test_me_with_valid_token(self) -> None:
        from service.portal.auth.dev_issuer import issue_dev_token

        token = issue_dev_token(user_id="alice")
        me_resp = self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_resp.status_code, 200)
        body = me_resp.json()
        self.assertEqual(body["user_id"], "alice")
        self.assertEqual(body["clearance"], "authorized")


class TestPortalAuthDevToken(unittest.TestCase):
    """042 dev /auth/token — auth disabled 일 때만."""

    def setUp(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        _reset_verifier_for_tests()
        self._env = patch.dict(
            os.environ,
            {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": "test-secret"},
            clear=False,
        )
        self._env.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        from service.portal.auth.verifier import _reset_verifier_for_tests

        self._env.stop()
        _reset_verifier_for_tests()

    def test_auth_token_issues_jwt(self) -> None:
        token_resp = self.client.post("/auth/token", json={"username": "alice"})
        self.assertEqual(token_resp.status_code, 200)
        token = token_resp.json()["access_token"]
        me_resp = self.client.get("/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me_resp.json()["clearance"], "authorized")


class TestPortalOpenApiSecurity(unittest.TestCase):
    """Swagger /docs — HTTPBearer Authorize 버튼(OpenAPI securitySchemes)."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_openapi_exposes_http_bearer_security(self) -> None:
        spec = self.client.get("/openapi.json").json()
        schemes = spec.get("components", {}).get("securitySchemes", {})
        self.assertIn("HTTPBearer", schemes)
        self.assertEqual(schemes["HTTPBearer"]["scheme"], "bearer")
        search = spec["paths"]["/search"]["get"]
        self.assertIn({"HTTPBearer": []}, search.get("security", []))
        params = search.get("parameters", [])
        self.assertFalse(any(p.get("name") == "authorization" for p in params))


class TestAssetThumbnail(unittest.TestCase):
    """GET /assets/{id}/thumbnail — 썸네일 게이트(의료 배제·유형·파일 부재)·응답 계약(057-후속)."""

    def setUp(self) -> None:
        _enable_portal_test_auth_bypass(self)
        self.client = TestClient(app)

    @patch("service.api.routes_assets.cached_thumbnail", return_value=b"\xff\xd8\xff\xe0JPG")
    @patch("service.api.routes_assets.resolve_download_target")
    def test_image_returns_jpeg(self, mock_resolve, _gen) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            mock_resolve.return_value = {
                "asset_id": "a1", "fs_path": f.name, "modality": "image", "file_name": "a.png"}
            r = self.client.get("/assets/a1/thumbnail")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertEqual(r.content, b"\xff\xd8\xff\xe0JPG")
        self.assertIn("max-age", r.headers.get("cache-control", ""))

    @patch("service.api.routes_assets.cached_thumbnail", return_value=b"HERO")
    @patch("service.api.routes_assets.resolve_download_target")
    def test_size_query_passed_through(self, mock_resolve, mock_cached) -> None:
        # ?size=detail → cached_thumbnail(size="detail") 로 전달(상세 히어로 640).
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            mock_resolve.return_value = {"asset_id": "a1", "fs_path": f.name, "modality": "image"}
            r = self.client.get("/assets/a1/thumbnail?size=detail")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(mock_cached.call_args.kwargs.get("size"), "detail")

    @patch("service.api.routes_assets.resolve_download_target", return_value=None)
    def test_medical_or_missing_returns_404(self, _resolve) -> None:
        # 의료/비registered/없음 → resolve_download_target None → 404 (의료 썸네일=PHI 원천 차단)
        self.assertEqual(self.client.get("/assets/a1/thumbnail").status_code, 404)

    @patch("service.api.routes_assets.resolve_download_target")
    def test_audio_returns_404(self, mock_resolve) -> None:
        mock_resolve.return_value = {"asset_id": "a1", "fs_path": "/x/a.mp3", "modality": "audio"}
        self.assertEqual(self.client.get("/assets/a1/thumbnail").status_code, 404)

    @patch("service.api.routes_assets.resolve_download_target")
    def test_missing_file_returns_410(self, mock_resolve) -> None:
        mock_resolve.return_value = {
            "asset_id": "a1", "fs_path": "/nonexistent/x.png", "modality": "image"}
        self.assertEqual(self.client.get("/assets/a1/thumbnail").status_code, 410)

    @patch("service.api.routes_assets.cached_thumbnail", return_value=None)
    @patch("service.api.routes_assets.resolve_download_target")
    def test_generation_failure_returns_404(self, mock_resolve, _gen) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            mock_resolve.return_value = {"asset_id": "a1", "fs_path": f.name, "modality": "image"}
            r = self.client.get("/assets/a1/thumbnail")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
