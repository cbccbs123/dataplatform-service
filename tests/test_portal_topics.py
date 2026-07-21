"""056 G7 — 포털 주제 표면 단위 테스트 (FR-501/502/503-facet/505).

전략(test_portal_api.py 패턴 재사용)
    FastAPI ``TestClient`` + auth bypass + ``_run_in_db`` passthrough. 주제 seam(자기주제 정본·065:
    ``fetch_asset_topic``/``find_same_topic_groups``/``list_topics``/``assets_in_topic``)과
    검색 seam(``search_hybrid``)을 ``patch`` 로 대체해 **DB·OS·LLM·네트워크 없이** 순수 단위로 돈다.

검증 대상
    - 자산상세(``GET /assets/{id}``): 응답에 ``topics``(fetch_asset_topic 자기주제 정본) +
      ``same_topic_groups``(find_same_topic_groups·공유 주제별 그룹·``already_linked`` 포함) 동반.
      노출 게이트(None→404) 보존.
    - ``GET /topics`` → list_topics · ``GET /topics/{topic}`` → assets_in_topic 페이징(subtopic·limit·offset 전달).
    - ``GET /search`` → 응답 meta 에 주제 패싯 집계(``topic_facets``) · ``topic=``/``subtopic=`` 파라미터가
      parse_search_filters 를 거쳐 search_hybrid 의 ``search_filters`` 로 전달.
    - 전부 **신규 LLM 호출 0**(주제 seam·검색 seam 만).
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import app

_AUTH_DISABLED_ENV = {
    "PORTAL_AUTH_DISABLED": "1",
    "PORTAL_JWT_SECRET": "test-secret",
}


def _passthrough_db(callback):
    """``_run_in_db`` 대역 — 가짜 conn 으로 callback 즉시 실행(DB 불필요·조회 함수는 patch)."""
    return callback(object())


def _empty_tiers(*_args, **_kwargs):
    return {}


def _enable_bypass(test_case: unittest.TestCase) -> None:
    env = patch.dict(os.environ, _AUTH_DISABLED_ENV, clear=False)
    env.start()
    test_case.addCleanup(env.stop)
    db = patch("service.api._infra._run_in_db", _passthrough_db)
    db.start()
    test_case.addCleanup(db.stop)


def _fake_search_result() -> dict:
    return {
        "query": "요리",
        "results": {
            "text_documents": [
                # 057-후속: 결과 행에 색인 topics 포함(패싯·클라 좁히기 소스 = 필터와 동일).
                # 060: topic_pairs(부모>자식 짝)로 nested 집계(교차곱 제거). 단일 topic 이라 결과 불변(SC-02).
                {"id": "a1", "similarity": 0.9, "file_uri": "/x/a1.txt", "summary": "s1",
                 "topics": ["요리"], "subtopics": ["제빵"], "topic_pairs": ["요리>제빵"]},
                {"id": "a2", "similarity": 0.8, "file_uri": "/x/a2.txt", "summary": "s2",
                 "topics": ["요리"], "subtopics": [], "topic_pairs": ["요리"]},
                {"id": "a3", "similarity": 0.7, "file_uri": "/x/a3.txt", "summary": "s3",
                 "topics": ["스포츠"], "subtopics": [], "topic_pairs": ["스포츠"]},
            ],
        },
        "meta": {},
    }


class TestAssetDetailTopics(unittest.TestCase):
    """GET /assets/{id} — topics + same_topic_groups 보강(FR-501·057-후속 그룹화)."""

    def setUp(self) -> None:
        _enable_bypass(self)
        self.client = TestClient(app)

    @patch("service.api.routes_assets.find_same_topic_groups")
    @patch("service.api.routes_assets.fetch_asset_topic")
    @patch("service.api.routes_assets.fetch_asset_detail")
    def test_detail_includes_topics_and_same_topic(
        self, mock_detail, mock_project, mock_groups
    ) -> None:
        mock_detail.return_value = {
            "asset_id": "a1", "modality": "text", "domain_label": "general",
            "status": "registered", "relations": [],
        }
        mock_project.return_value = [
            {"topic_ko": "요리", "subtopic_ko": "제빵", "topic_en": "cooking",
             "subtopic_en": "baking", "weight": 1},
        ]
        # 057-후속: 공유 주제(topic_ko)→하위주제(subtopic_ko) 2단 중첩 — "무슨 주제·하위주제로 같은지".
        mock_groups.return_value = [
            {"topic_ko": "요리", "asset_count": 2, "subtopics": [
                {"subtopic_ko": "제빵", "asset_count": 2, "assets": [
                    {"asset_id": "a2", "file_name": "a2.png", "modality": "image",
                     "already_linked": True},
                    {"asset_id": "a7", "file_name": "a7.txt", "modality": "text",
                     "already_linked": False},
                ]},
            ]},
        ]
        resp = self.client.get("/assets/a1")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["topics"], mock_project.return_value)
        self.assertEqual(body["same_topic_groups"], mock_groups.return_value)
        # 그룹→하위주제 구조·already_linked 표식 보존
        self.assertEqual(body["same_topic_groups"][0]["topic_ko"], "요리")
        sub0 = body["same_topic_groups"][0]["subtopics"][0]
        self.assertEqual(sub0["subtopic_ko"], "제빵")
        self.assertTrue(sub0["assets"][0]["already_linked"])
        self.assertFalse(sub0["assets"][1]["already_linked"])
        # seam 은 대상 자산으로 조회
        self.assertEqual(mock_project.call_args.kwargs["asset_id"], "a1")
        self.assertEqual(mock_groups.call_args.kwargs["asset_id"], "a1")

    @patch("service.api.routes_assets.find_same_topic_groups")
    @patch("service.api.routes_assets.fetch_asset_topic")
    @patch("service.api.routes_assets.fetch_asset_detail")
    def test_detail_none_returns_404_no_topic_calls(
        self, mock_detail, mock_project, mock_groups
    ) -> None:
        # 노출 게이트: fetch_asset_detail None → 404, 주제 seam 미호출(불필요 조회 없음).
        mock_detail.return_value = None
        resp = self.client.get("/assets/nope")
        self.assertEqual(resp.status_code, 404)
        mock_project.assert_not_called()
        mock_groups.assert_not_called()


class TestTopicsList(unittest.TestCase):
    """GET /topics — list_topics 위임(FR-502)."""

    def setUp(self) -> None:
        _enable_bypass(self)
        self.client = TestClient(app)

    @patch("service.api.routes_assets.list_topics")
    def test_list_topics(self, mock_list) -> None:
        mock_list.return_value = [
            {"topic_ko": "요리", "subtopic_ko": "제빵", "asset_count": 12},
            {"topic_ko": "스포츠", "subtopic_ko": None, "asset_count": 5},
        ]
        resp = self.client.get("/topics")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["topics"], mock_list.return_value)
        mock_list.assert_called_once()


class TestTopicAssets(unittest.TestCase):
    """GET /topics/{topic} — assets_in_topic 페이징(FR-502)."""

    def setUp(self) -> None:
        _enable_bypass(self)
        self.client = TestClient(app)

    @patch("service.api.routes_assets.assets_in_topic")
    def test_topic_assets_paging(self, mock_assets) -> None:
        mock_assets.return_value = {
            "rows": [{"asset_id": "a1", "fs_uri": "/x/a1.txt", "file_name": "a1.txt"}],
            "total": 1,
        }
        resp = self.client.get(
            "/topics/요리", params={"subtopic": "제빵", "limit": 10, "offset": 5}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), mock_assets.return_value)
        kw = mock_assets.call_args.kwargs
        self.assertEqual(kw["topic_ko"], "요리")
        self.assertEqual(kw["subtopic_ko"], "제빵")
        self.assertEqual(kw["limit"], 10)
        self.assertEqual(kw["offset"], 5)

    @patch("service.api.routes_assets.assets_in_topic")
    def test_topic_assets_no_subtopic(self, mock_assets) -> None:
        mock_assets.return_value = {"rows": [], "total": 0}
        resp = self.client.get("/topics/스포츠")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(mock_assets.call_args.kwargs["subtopic_ko"])


class TestSearchTopicFacetAndFilter(unittest.TestCase):
    """GET /search — 주제 패싯 집계 + topic/subtopic 필터 전달(FR-503)."""

    def setUp(self) -> None:
        _enable_bypass(self)
        tiers = patch("service.api.routes_search.fetch_access_tiers", side_effect=_empty_tiers)
        tiers.start()
        self.addCleanup(tiers.stop)
        self.client = TestClient(app)

    @patch("service.api.routes_search.search_hybrid")
    def test_search_returns_topic_facet(self, mock_search) -> None:
        # 057-후속: 패싯은 결과 행의 **색인 topics**(=필터 소스)로 집계 — project_asset_topics 미사용
        # (라이브 투영 대비 소스 불일치·N+1 제거). 프론트는 이 행 topics 로 클라 좁히기.
        mock_search.return_value = _fake_search_result()
        resp = self.client.get("/search", params={"q": "요리", "size": 10})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        facets = body["meta"]["topic_facets"]
        # 요리={a1,a2} 2건(하위 제빵 a1 1건), 스포츠={a3} 1건(하위 없음). 결과-스코프 nested·결정적 정렬.
        self.assertEqual(
            facets,
            [
                {"topic_ko": "요리", "asset_count": 2,
                 "subtopics": [{"subtopic_ko": "제빵", "asset_count": 1}]},
                {"topic_ko": "스포츠", "asset_count": 1, "subtopics": []},
            ],
        )
        # 결과 행에도 topics 노출(프론트 클라 좁히기용) → 패싯 클릭 = 이 topics 로 필터.
        rows = [r for bucket in body["results"].values() for r in bucket]
        self.assertTrue(any(r.get("topics") == ["요리"] for r in rows))

    @patch("service.api.routes_search.search_hybrid")
    def test_facet_pairs_no_cross_product(self, mock_search) -> None:
        # 060 SC-01: 멀티토픽 자산 — 평면 topics=[요리,IT·기술]×subtopics=[제빵,데이터] 를 교차곱하면
        # 요리>데이터·IT·기술>제빵 오배치가 난다. topic_pairs 짝으로 집계하면 각자 올바른 부모 아래로만.
        mock_search.return_value = {
            "query": "요리", "meta": {},
            "results": {"text_documents": [
                {"id": "m1", "similarity": 0.9, "file_uri": "/x/m1.txt", "summary": "s",
                 "topics": ["요리", "IT·기술"], "subtopics": ["제빵", "데이터"],
                 "topic_pairs": ["요리>제빵", "IT·기술>데이터"]},
            ]},
        }
        resp = self.client.get("/search", params={"q": "요리", "size": 10})
        self.assertEqual(resp.status_code, 200)
        facets = resp.json()["meta"]["topic_facets"]
        self.assertEqual(
            facets,
            [
                {"topic_ko": "IT·기술", "asset_count": 1,
                 "subtopics": [{"subtopic_ko": "데이터", "asset_count": 1}]},
                {"topic_ko": "요리", "asset_count": 1,
                 "subtopics": [{"subtopic_ko": "제빵", "asset_count": 1}]},
            ],
        )
        # 교차곱 오배치가 없어야 한다(요리 밑에 데이터, IT·기술 밑에 제빵 금지).
        by_topic = {f["topic_ko"]: [s["subtopic_ko"] for s in f["subtopics"]] for f in facets}
        self.assertNotIn("데이터", by_topic["요리"])
        self.assertNotIn("제빵", by_topic["IT·기술"])

    @patch("service.api.routes_search.search_hybrid")
    def test_facet_fallback_without_pairs(self, mock_search) -> None:
        # 060 SC-03: topic_pairs 부재(미재색인 인덱스) → topic 카운트만·nested 비움·예외 없음(오배치 0).
        mock_search.return_value = {
            "query": "요리", "meta": {},
            "results": {"text_documents": [
                {"id": "f1", "similarity": 0.9, "file_uri": "/x/f1.txt", "summary": "s",
                 "topics": ["요리"], "subtopics": ["제빵"]},  # topic_pairs 없음
            ]},
        }
        resp = self.client.get("/search", params={"q": "요리", "size": 10})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["meta"]["topic_facets"],
            [{"topic_ko": "요리", "asset_count": 1, "subtopics": []}],
        )

    @patch("service.api.routes_search.search_hybrid")
    def test_facet_pair_splits_on_first_gt(self, mock_search) -> None:
        # 060/059 계약(PR #85 리뷰 🟡): 첫 '>' 로만 분할 — subtopic 에 '>' 가 섞여도 부모(topic)는
        # 항상 정확(topic 층은 닫힌 통제어휘·'>' 미포함)·부모 오배치 없음. subtopic 표기는 그대로 보존.
        mock_search.return_value = {
            "query": "요리", "meta": {},
            "results": {"text_documents": [
                {"id": "g1", "similarity": 0.9, "file_uri": "/x/g1.txt", "summary": "s",
                 "topics": ["요리"], "subtopics": ["제빵>홈베이킹"],
                 "topic_pairs": ["요리>제빵>홈베이킹"]},
            ]},
        }
        resp = self.client.get("/search", params={"q": "요리", "size": 10})
        self.assertEqual(
            resp.json()["meta"]["topic_facets"],
            [{"topic_ko": "요리", "asset_count": 1,
              "subtopics": [{"subtopic_ko": "제빵>홈베이킹", "asset_count": 1}]}],
        )

    @patch("service.api.routes_search.search_hybrid")
    def test_facet_mixed_index_pairs_and_fallback(self, mock_search) -> None:
        # 060(PR #85 리뷰 🟡): 재색인 도중 신/구 인덱스 혼합 — 같은 topic 이 한 행은 짝 기준(제빵 nested),
        # 다른 행은 topic_pairs 부재(폴백·nested 미기여). topic 카운트는 두 자산 합산, nested 는 짝 있는 자산만.
        mock_search.return_value = {
            "query": "요리", "meta": {},
            "results": {"text_documents": [
                {"id": "x1", "similarity": 0.9, "file_uri": "/x/x1.txt", "summary": "s",
                 "topics": ["요리"], "subtopics": ["제빵"], "topic_pairs": ["요리>제빵"]},
                {"id": "x2", "similarity": 0.8, "file_uri": "/x/x2.txt", "summary": "s",
                 "topics": ["요리"], "subtopics": ["제빵"]},  # topic_pairs 없음(구 인덱스)
            ]},
        }
        resp = self.client.get("/search", params={"q": "요리", "size": 10})
        self.assertEqual(
            resp.json()["meta"]["topic_facets"],
            [{"topic_ko": "요리", "asset_count": 2,
              "subtopics": [{"subtopic_ko": "제빵", "asset_count": 1}]}],
        )

    @patch("service.api.routes_search.search_hybrid")
    def test_search_passes_topic_filter(self, mock_search) -> None:
        mock_search.return_value = _fake_search_result()
        resp = self.client.get(
            "/search", params={"q": "요리", "topic": "요리", "subtopic": "제빵"}
        )
        self.assertEqual(resp.status_code, 200)
        sf = mock_search.call_args.kwargs["search_filters"]
        self.assertIsNotNone(sf)
        self.assertEqual(sf.topic, "요리")
        self.assertEqual(sf.subtopic, "제빵")


if __name__ == "__main__":
    unittest.main()
