"""포탈 자산 상세 조회(``fetch_asset_detail``) mock conn 단위 테스트 (DB 불필요).

검증 의도 (plan 010 D-3)
    - FR-004: ``core_meta``/``ext_meta`` 를 **구분**해 반환(병합하지 않음).
    - FR-005: 임베딩은 채널별 청크 **개수만**(``embedding_channels=[{channel,chunk_count}]``),
      원시 벡터(VECTOR 1536) 미노출. 집계 SQL 이 ``COUNT(*)``·``GROUP BY/ORDER BY channel`` 인지도 검사.
    - FR-006: 관계는 ``graph_query.fetch_active_relations_for_asset`` 결과(양방향) — 주입/모킹.
    - FR-014 노출 게이트: 행 없음 / ``status!='registered'`` / ``domain_label='medical'`` → ``None``.
    - 헌법 3조: 동일 입력 2회 동일 출력.

mock conn 패턴은 test_graph_query / relation_type_catalog 와 동형(``cursor(row_factory=dict_row)``
컨텍스트매니저). ``fetch_active_relations_for_asset`` 는 asset_detail 네임스페이스에서 patch 한다.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


def _conn_for_detail(asset_row, channel_rows):
    """``conn.cursor(row_factory=dict_row)`` 컨텍스트매니저를 흉내내는 mock conn.

    같은 cur 가 두 번 쓰인다: ① asset+metadata 조회는 ``fetchone`` ② 임베딩 채널 집계는
    ``fetchall``. 둘은 서로 다른 메서드라 한 cur 에 모두 세팅해도 충돌하지 않는다.
    ``execute`` 인자는 call_args_list 로 캡처해 SQL 검사에 쓴다.
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchone.return_value = asset_row
    cur.fetchall.return_value = channel_rows
    conn.cursor.return_value = cur
    return conn, cur


_REGISTERED_ROW = {
    "asset_id": "A1",
    "modality": "text",
    "domain_label": "general",
    "status": "registered",
    "fs_path": "/data/raw/보고서.pdf",
    "core_meta": {"title": "보고서"},
    "ext_meta": {"pages": 12},
    "tags": ["report", "2026"],
}

_RELATIONS = [
    {
        "asset_id": "B2", "kind_code": "duplicate_near", "is_symmetric": True,
        "direction": "undirected", "confidence": 0.9, "status": "active",
        "topic": {"topic_ko": "사진"}, "reason": "유사", "edge_id": "e1",
        # FR-102(057·G1): 이웃 표시필드는 이미 엣지 dict 에 내려온다(재조회 0). 병합 시 이웃 레벨로 승격.
        "file_name": "b2.jpg", "modality": "image",
    },
]

# FR-201(057·G2a) 병합 검증용 — 같은 이웃(B2)과 다중 엣지 + 다른 이웃(A0)이 섞인 이웃-엣지 목록.
# graph_query.fetch_active_relations_for_asset 가 주는 엣지 단위 목록의 실제 모양을 흉내낸다.
_RELATIONS_MULTI = [
    # 이웃 B2 — 같은 이웃과 2개 엣지(다른 kind·다른 confidence·삽입순서 뒤섞음) → 1행으로 병합돼야.
    {"asset_id": "B2", "kind_code": "same_topic", "is_symmetric": True,
     "direction": "undirected", "confidence": 0.7, "status": "active",
     "topic": {"topic_ko": "사진"}, "reason": "주제겹침", "edge_id": "e2",
     "file_name": "b2.jpg", "modality": "image"},
    {"asset_id": "B2", "kind_code": "duplicate_near", "is_symmetric": True,
     "direction": "undirected", "confidence": 0.9, "status": "active",
     "topic": {"topic_ko": "사진"}, "reason": "유사", "edge_id": "e1",
     "file_name": "b2.jpg", "modality": "image"},
    # 이웃 A0 — max_confidence 0.95(> B2 0.9) → 이웃 정렬상 앞에 와야.
    {"asset_id": "A0", "kind_code": "derived_from", "is_symmetric": False,
     "direction": "outbound", "confidence": 0.95, "status": "active",
     "topic": None, "reason": "파생", "edge_id": "e3",
     "file_name": "a0.pdf", "modality": "text"},
]


class TestFetchAssetDetail(unittest.TestCase):
    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_happy_path_separates_core_and_ext_meta(self, mock_rel) -> None:
        # FR-004: core_meta/ext_meta 가 별개 키로 반환(병합 금지).
        mock_rel.return_value = list(_RELATIONS)
        conn, _ = _conn_for_detail(
            dict(_REGISTERED_ROW),
            [{"channel": "image_clip", "chunk_count": 3}, {"channel": "text", "chunk_count": 5}],
        )
        from service.portal.asset_detail import fetch_asset_detail

        out = fetch_asset_detail(conn, asset_id="A1")
        self.assertIsNotNone(out)
        self.assertEqual(out["asset_id"], "A1")
        self.assertEqual(out["modality"], "text")
        self.assertEqual(out["domain_label"], "general")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(out["core_meta"], {"title": "보고서"})
        self.assertEqual(out["ext_meta"], {"pages": 12})
        self.assertEqual(out["tags"], ["report", "2026"])

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_top_level_file_name_from_fs_path_basename(self, mock_rel) -> None:
        # FR-101(057): 상세 응답 최상위 file_name = fs_path basename(search_group.display_name 단일 출처).
        # → web A3·admin A1 다운로드 프리플라이트 워크어라운드 제거 기반(N+1 소멸).
        mock_rel.return_value = []
        conn, _ = _conn_for_detail(dict(_REGISTERED_ROW), [])
        from service.portal.asset_detail import fetch_asset_detail

        out = fetch_asset_detail(conn, asset_id="A1")
        self.assertEqual(out["file_name"], "보고서.pdf")

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_embedding_channels_count_only_no_raw_vector(self, mock_rel) -> None:
        # FR-005: 채널별 청크 개수만, 각 dict 은 channel/chunk_count 키만(원시 벡터 없음).
        mock_rel.return_value = []
        conn, _ = _conn_for_detail(
            dict(_REGISTERED_ROW),
            [{"channel": "image_clip", "chunk_count": 3}, {"channel": "text", "chunk_count": 5}],
        )
        from service.portal.asset_detail import fetch_asset_detail

        out = fetch_asset_detail(conn, asset_id="A1")
        self.assertEqual(
            out["embedding_channels"],
            [{"channel": "image_clip", "chunk_count": 3}, {"channel": "text", "chunk_count": 5}],
        )
        for ch in out["embedding_channels"]:
            self.assertEqual(set(ch.keys()), {"channel", "chunk_count"})

    @patch("service.portal.asset_detail.fetch_access_tiers")
    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_clearance_projects_ext_meta(self, mock_rel, mock_tiers) -> None:
        mock_rel.return_value = []
        mock_tiers.return_value = {"summary": "authenticated", "stt": "authorized"}
        row = dict(_REGISTERED_ROW)
        row["ext_meta"] = {"summary": "요약", "stt": "전문"}
        conn, _ = _conn_for_detail(row, [])
        from service.portal.asset_detail import fetch_asset_detail
        from src.registry.access_tier import AUTHORIZED, PUBLIC

        anon = fetch_asset_detail(conn, asset_id="A1", clearance=PUBLIC)
        self.assertEqual(anon["ext_meta"], {})
        auth = fetch_asset_detail(conn, asset_id="A1", clearance=AUTHORIZED)
        self.assertEqual(auth["ext_meta"], {"summary": "요약", "stt": "전문"})

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_embedding_query_is_count_aggregate_not_raw_select(self, mock_rel) -> None:
        # 집계 SQL 이 COUNT(*)·GROUP BY/ORDER BY channel 인지(원시 벡터 SELECT 금지, FR-005·헌법 6조).
        mock_rel.return_value = []
        conn, cur = _conn_for_detail(dict(_REGISTERED_ROW), [{"channel": "text", "chunk_count": 2}])
        from service.portal.asset_detail import fetch_asset_detail

        fetch_asset_detail(conn, asset_id="A1")
        sqls = [" ".join(c[0][0].split()) for c in cur.execute.call_args_list]
        agg = next(s for s in sqls if "asset_embedding" in s)
        self.assertIn("COUNT(*)", agg)
        self.assertIn("GROUP BY channel", agg)
        self.assertIn("ORDER BY channel", agg)
        # 원시 벡터 컬럼을 직접 SELECT 하지 않는다.
        self.assertNotIn("SELECT embedding", agg)

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_relations_merged_by_asset_from_graph_query_seam(self, mock_rel) -> None:
        # FR-006/FR-201(057): 엣지 단위 seam(fetch_active_relations_for_asset) 은 그대로 asset_id
        #   키워드로 1회 호출하되, 상세 응답의 relations 는 그 결과를 이웃 자산 단위로 사전 병합한다.
        #   (프론트 mergeRelationsByAsset 재구현 제거 — 결정적 병합을 서버 단일 진실로.)
        mock_rel.return_value = list(_RELATIONS)
        conn, _ = _conn_for_detail(dict(_REGISTERED_ROW), [])
        from service.portal.asset_detail import fetch_asset_detail

        out = fetch_asset_detail(conn, asset_id="A1")
        mock_rel.assert_called_once_with(conn, asset_id="A1")
        # 엣지 1건 → 이웃 1행(병합). 표시필드는 이웃 레벨로 승격, 엣지 상세는 edges 에 보존.
        self.assertEqual(len(out["relations"]), 1)
        nb = out["relations"][0]
        self.assertEqual(nb["asset_id"], "B2")
        self.assertEqual(nb["file_name"], "b2.jpg")
        self.assertEqual(nb["modality"], "image")
        self.assertEqual(nb["kind_codes"], ["duplicate_near"])
        self.assertEqual(nb["max_confidence"], 0.9)
        self.assertEqual(
            nb["edges"],
            [{
                "edge_id": "e1", "kind_code": "duplicate_near", "confidence": 0.9,
                "direction": "undirected", "is_symmetric": True,
                "topic": {"topic_ko": "사진"}, "reason": "유사",
            }],
        )

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_relations_merge_dedup_kinds_maxconf_edges_and_sort(self, mock_rel) -> None:
        # FR-201: 같은 이웃(B2)과 다중 엣지 → 1행 병합. kind_codes distinct·오름차순, max_confidence=엣지 최대,
        #   edges 상세 보존(confidence desc→edge_id asc). 이웃 정렬 max_confidence desc→asset_id asc.
        mock_rel.return_value = list(_RELATIONS_MULTI)
        conn, _ = _conn_for_detail(dict(_REGISTERED_ROW), [])
        from service.portal.asset_detail import fetch_asset_detail

        rels = fetch_asset_detail(conn, asset_id="A1")["relations"]
        # 3 엣지 → 이웃 2행(B2 2엣지 병합). 정렬: A0(max 0.95) → B2(max 0.9).
        self.assertEqual([n["asset_id"] for n in rels], ["A0", "B2"])
        b2 = rels[1]
        self.assertEqual(b2["kind_codes"], ["duplicate_near", "same_topic"])  # distinct·오름차순
        self.assertEqual(b2["max_confidence"], 0.9)  # max(0.9, 0.7)
        self.assertEqual([e["edge_id"] for e in b2["edges"]], ["e1", "e2"])  # confidence desc
        self.assertEqual(b2["file_name"], "b2.jpg")
        self.assertEqual(b2["modality"], "image")
        # 엣지 dict 은 정확히 7개 상세 키 — asset_id/file_name/modality/status 는 이웃 레벨로 승격.
        self.assertEqual(
            set(b2["edges"][0].keys()),
            {"edge_id", "kind_code", "confidence", "direction", "is_symmetric", "topic", "reason"},
        )
        # A0(비대칭·outbound) 도 동일 계약.
        a0 = rels[0]
        self.assertEqual(a0["kind_codes"], ["derived_from"])
        self.assertEqual(a0["max_confidence"], 0.95)
        self.assertEqual(a0["edges"][0]["direction"], "outbound")

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_relations_sort_tiebreak_asset_id_and_null_confidence_last(self, mock_rel) -> None:
        # 결정성(헌법 3조): 동점 max_confidence → asset_id asc. confidence None 이웃은 NULLS LAST(맨 뒤).
        mock_rel.return_value = [
            {"asset_id": "Z9", "kind_code": "k", "is_symmetric": False, "direction": "outbound",
             "confidence": 0.5, "status": "active", "topic": None, "reason": None, "edge_id": "e1",
             "file_name": "z.txt", "modality": "text"},
            {"asset_id": "M5", "kind_code": "k", "is_symmetric": False, "direction": "outbound",
             "confidence": 0.5, "status": "active", "topic": None, "reason": None, "edge_id": "e2",
             "file_name": "m.txt", "modality": "text"},
            {"asset_id": "N0", "kind_code": "k", "is_symmetric": False, "direction": "outbound",
             "confidence": None, "status": "active", "topic": None, "reason": None, "edge_id": "e3",
             "file_name": "n.txt", "modality": "text"},
        ]
        conn, _ = _conn_for_detail(dict(_REGISTERED_ROW), [])
        from service.portal.asset_detail import fetch_asset_detail

        rels = fetch_asset_detail(conn, asset_id="A1")["relations"]
        # 0.5 동점(M5,Z9)은 asset_id asc → M5,Z9. confidence None(N0)은 최후.
        self.assertEqual([n["asset_id"] for n in rels], ["M5", "Z9", "N0"])
        self.assertIsNone(rels[2]["max_confidence"])  # 모든 엣지 None → max_confidence None

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_row_not_found_returns_none(self, mock_rel) -> None:
        # 행 없음 → None(API 404).
        mock_rel.return_value = []
        conn, _ = _conn_for_detail(None, [])
        from service.portal.asset_detail import fetch_asset_detail

        self.assertIsNone(fetch_asset_detail(conn, asset_id="ZZ"))

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_non_registered_returns_none(self, mock_rel) -> None:
        # status != 'registered'(failed/deferred) → None(FR-014/노출 게이트).
        mock_rel.return_value = []
        row = dict(_REGISTERED_ROW)
        row["status"] = "failed"
        conn, _ = _conn_for_detail(row, [])
        from service.portal.asset_detail import fetch_asset_detail

        self.assertIsNone(fetch_asset_detail(conn, asset_id="A1"))

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_medical_returns_none(self, mock_rel) -> None:
        # 의료 자산 배제(FR-014) → None.
        mock_rel.return_value = []
        row = dict(_REGISTERED_ROW)
        row["domain_label"] = "medical"
        conn, _ = _conn_for_detail(row, [])
        from service.portal.asset_detail import fetch_asset_detail

        self.assertIsNone(fetch_asset_detail(conn, asset_id="A1"))

    @patch("service.portal.asset_detail.fetch_active_relations_for_asset")
    def test_determinism_same_input_same_output(self, mock_rel) -> None:
        # 헌법 3조: 동일 입력 2회 동일 출력.
        mock_rel.return_value = list(_RELATIONS)
        from service.portal.asset_detail import fetch_asset_detail

        conn1, _ = _conn_for_detail(dict(_REGISTERED_ROW), [{"channel": "text", "chunk_count": 5}])
        conn2, _ = _conn_for_detail(dict(_REGISTERED_ROW), [{"channel": "text", "chunk_count": 5}])
        self.assertEqual(
            fetch_asset_detail(conn1, asset_id="A1"),
            fetch_asset_detail(conn2, asset_id="A1"),
        )


if __name__ == "__main__":
    unittest.main()
