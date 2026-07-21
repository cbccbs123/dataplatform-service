"""포탈 검색 모달리티 그룹화(``group_ranked``) 순수 단위 테스트.

DB·네트워크·파일 IO 없이 완전 순수. 모달리티별로 **독립 랭킹**(cross-modal 병합 없음)을
검증한다 — 같은 video 점수가 text 보다 구조적으로 높아도 video 가 text 섹션을 침범하지
않는다(원인 ③ 회피). 헌법 3조(결정성)는 버킷 내 (-round(sim,6), asset_id) tiebreak 으로 검증.
"""
from __future__ import annotations

import math
import unittest


def _result(buckets: dict) -> dict:
    """search_service.search_hybrid 반환 형태(query/results/meta)를 흉내낸 입력."""
    return {"query": "q", "results": buckets, "meta": {}}


class TestGroupRanked(unittest.TestCase):
    def test_grouped_by_modality_no_cross_merge(self) -> None:
        # video 점수(0.9)가 text(0.5)보다 높아도 두 모달리티는 별도 섹션에 머문다 —
        # 단일 랭킹으로 합치지 않으므로 video 가 text 를 침범하지 않는다(원인 ③ 회피).
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "text_documents": [
                    {"id": "t1", "similarity": 0.50, "file_uri": "/x/t1.txt", "summary": "T1"},
                ],
                "video": [
                    {"id": "v1", "similarity": 0.90, "file_uri": "/x/v1.mp4", "summary": "V1"},
                ],
            }
        )
        grouped = group_ranked(result, limit_per_modality=20)
        self.assertEqual(set(grouped.keys()), {"text", "video"})
        self.assertEqual([r["asset_id"] for r in grouped["text"]], ["t1"])
        self.assertEqual([r["asset_id"] for r in grouped["video"]], ["v1"])

    def test_within_modality_sorted_desc_tiebreak_id(self) -> None:
        # 한 모달리티 안에서는 유사도 내림차순, 동점은 asset_id 오름차순(결정적).
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "text_documents": [
                    {"id": "a", "similarity": 0.50, "file_uri": "a.txt", "summary": ""},
                    {"id": "c", "similarity": 0.90, "file_uri": "c.txt", "summary": ""},
                    {"id": "b", "similarity": 0.90, "file_uri": "b.txt", "summary": ""},
                ],
            }
        )
        ids = [r["asset_id"] for r in group_ranked(result, limit_per_modality=20)["text"]]
        self.assertEqual(ids, ["b", "c", "a"])  # 0.9 동점 b<c, 그다음 0.5 a

    def test_item_shape_and_modality_label(self) -> None:
        # 각 항목은 asset_id/modality/similarity/summary/file_name/domain_label, 버킷키→모달리티 라벨.
        from service.portal.search_group import group_ranked

        result = _result(
            {"text_documents": [
                {
                    "id": "a",
                    "similarity": 0.5,
                    "file_uri": "/d/sub/report.txt",
                    "summary": "S",
                    "domain_label": "review",
                    "topics": ["요리"],
                    "subtopics": ["제빵"],
                    "topic_pairs": ["요리>제빵"],
                }
            ]}
        )
        item = group_ranked(result, limit_per_modality=20)["text"][0]
        self.assertEqual(
            set(item.keys()),
            {"asset_id", "modality", "similarity", "summary", "file_name", "domain_label",
             "topics", "subtopics",  # 057-후속: 주제 패싯·클라 좁히기용 통과
             "topic_pairs"},  # 059: 부모>자식 짝(프론트 트리) 통과
        )
        self.assertEqual(item["modality"], "text")
        self.assertEqual(item["domain_label"], "review")
        self.assertEqual(item["file_name"], "report.txt")
        self.assertEqual(item["summary"], "S")
        self.assertEqual(item["topics"], ["요리"])
        self.assertEqual(item["subtopics"], ["제빵"])
        self.assertEqual(item["topic_pairs"], ["요리>제빵"])

    def test_topic_pairs_default_empty_when_absent(self) -> None:
        # 059: topic_pairs 없는 행(하위호환)도 안전하게 빈 리스트로 통과한다.
        from service.portal.search_group import group_ranked

        result = _result({"text_documents": [{"id": "a", "similarity": 0.5}]})
        item = group_ranked(result, limit_per_modality=20)["text"][0]
        self.assertEqual(item["topic_pairs"], [])

    def test_all_modality_labels(self) -> None:
        # 4개 버킷 키가 각각 text/audio/image/video 섹션으로 매핑된다.
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "text_documents": [{"id": "t", "similarity": 0.4, "file_uri": "t.txt", "summary": ""}],
                "audio": [{"id": "u", "similarity": 0.3, "file_uri": "u.wav", "summary": ""}],
                "image": [{"id": "i", "similarity": 0.2, "file_uri": "i.png", "summary": ""}],
                "video": [{"id": "v", "similarity": 0.1, "file_uri": "v.mp4", "summary": ""}],
            }
        )
        grouped = group_ranked(result, limit_per_modality=20)
        self.assertEqual(set(grouped.keys()), {"text", "audio", "image", "video"})
        self.assertEqual(grouped["text"][0]["asset_id"], "t")
        self.assertEqual(grouped["audio"][0]["asset_id"], "u")
        self.assertEqual(grouped["image"][0]["asset_id"], "i")
        self.assertEqual(grouped["video"][0]["asset_id"], "v")

    def test_limit_per_modality_caps_each_bucket_independently(self) -> None:
        # 상한은 모달리티별로 독립 적용된다(한 버킷이 많아도 다른 버킷에 영향 없음).
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "text_documents": [
                    {"id": f"t{i}", "similarity": 1.0 - i * 0.01, "file_uri": f"t{i}.txt", "summary": ""}
                    for i in range(10)
                ],
                "image": [
                    {"id": f"i{i}", "similarity": 1.0 - i * 0.01, "file_uri": f"i{i}.png", "summary": ""}
                    for i in range(10)
                ],
            }
        )
        grouped = group_ranked(result, limit_per_modality=3)
        self.assertEqual(len(grouped["text"]), 3)
        self.assertEqual(len(grouped["image"]), 3)
        # 상위 3건이 점수 내림차순으로 남는다.
        self.assertEqual([r["asset_id"] for r in grouped["text"]], ["t0", "t1", "t2"])

    def test_exclude_domains_filters_per_bucket(self) -> None:
        # domain_label 이 exclude_domains 에 들면 해당 버킷에서 제외(FR-014), 없으면 포함.
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "image": [
                    {"id": "med", "similarity": 0.95, "file_uri": "m.png", "summary": "", "domain_label": "medical"},
                    {"id": "gen", "similarity": 0.80, "file_uri": "g.png", "summary": "", "domain_label": "general"},
                    {"id": "nolabel", "similarity": 0.70, "file_uri": "n.png", "summary": ""},
                ],
            }
        )
        ids = [
            r["asset_id"]
            for r in group_ranked(result, exclude_domains=frozenset({"medical"}), limit_per_modality=20)["image"]
        ]
        self.assertNotIn("med", ids)
        self.assertIn("gen", ids)
        self.assertIn("nolabel", ids)

    def test_nan_none_similarity_becomes_zero(self) -> None:
        # NaN/None similarity 는 0.0 으로 정화돼 버킷 맨 뒤로(유한값 보장).
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "text_documents": [
                    {"id": "good", "similarity": 0.7, "file_uri": "g.txt", "summary": ""},
                    {"id": "z_nan", "similarity": float("nan"), "file_uri": "n.txt", "summary": ""},
                    {"id": "a_none", "similarity": None, "file_uri": "x.txt", "summary": ""},
                ],
            }
        )
        rows = group_ranked(result, limit_per_modality=20)["text"]
        self.assertEqual([r["asset_id"] for r in rows], ["good", "a_none", "z_nan"])
        nan_item = next(r for r in rows if r["asset_id"] == "z_nan")
        self.assertTrue(math.isfinite(nan_item["similarity"]))
        self.assertEqual(nan_item["similarity"], 0.0)

    def test_only_present_modalities_returned(self) -> None:
        # results 에 있는 버킷만 키로 등장한다(modalities 필터 → 일부 섹션만).
        from service.portal.search_group import group_ranked

        result = _result(
            {"text_documents": [{"id": "t", "similarity": 0.5, "file_uri": "t.txt", "summary": ""}]}
        )
        grouped = group_ranked(result, limit_per_modality=20)
        self.assertEqual(set(grouped.keys()), {"text"})

    def test_deterministic_same_input_twice(self) -> None:
        # 동일 입력 2회 → 동일 그룹·순서(결정성, 헌법 3조).
        from service.portal.search_group import group_ranked

        result = _result(
            {
                "text_documents": [
                    {"id": "k2", "similarity": 0.5, "file_uri": "k2.txt", "summary": ""},
                    {"id": "k1", "similarity": 0.5, "file_uri": "k1.txt", "summary": ""},
                ],
            }
        )
        first = group_ranked(result, limit_per_modality=20)
        second = group_ranked(result, limit_per_modality=20)
        self.assertEqual(first, second)
        self.assertEqual([r["asset_id"] for r in first["text"]], ["k1", "k2"])

    def test_empty_result(self) -> None:
        # 빈 결과 → 빈 dict(경계).
        from service.portal.search_group import group_ranked

        self.assertEqual(group_ranked(_result({}), limit_per_modality=20), {})
        self.assertEqual(group_ranked({"query": "q", "meta": {}}, limit_per_modality=20), {})


if __name__ == "__main__":
    unittest.main()
