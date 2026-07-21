"""T304 (069 D3) — basename 단일 출처화 계약 테스트 (순수·DB 불필요).

``_basename`` 3벌(``search_group``·``sample_search_api``·``opensearch_sync``)의 **공통 코어**를
공개 ``basename_of(uri)`` 1벌로 통합한다. 3벌 중 ``search_group`` 만 추가로 asset_id 프리픽스를
벗겼는데(065 T605·표시 전용 책임), 그 strip 은 basename_of 위에 **합성**한다.

핵심(D3 최우선): **각 호출처의 관찰 출력이 통합 전후 완전히 동일**해야 한다.
    - basename_of 자체는 프리픽스를 벗기지 않는다(sample·opensearch_sync 와 동일).
    - search_group/asset_detail 표시명만 프리픽스를 벗긴다(변함없음).
"""

from __future__ import annotations

import unittest

_UUID = "018f0000-0000-7000-8000-000000000271"


class TestBasenameOfCore(unittest.TestCase):
    """공통 코어 ``basename_of`` — 쿼리(?)·프래그먼트(#)·백슬래시 정규화, 프리픽스 미제거."""

    def test_absolute_posix_path(self) -> None:
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of("/data/sub/report.pdf"), "report.pdf")

    def test_uri_query_stripped(self) -> None:
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of("https://h/a/file.jpg?x=1&y=2"), "file.jpg")

    def test_uri_fragment_stripped(self) -> None:
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of("/a/b/doc.txt#sec"), "doc.txt")

    def test_backslash_normalized(self) -> None:
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of(r"C:\Users\me\photo.png"), "photo.png")

    def test_trailing_slash(self) -> None:
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of("/a/b/"), "b")

    def test_empty(self) -> None:
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of(""), "")

    def test_asset_id_prefix_not_stripped(self) -> None:
        # 코어는 프리픽스를 벗기지 않는다 — 표시용 strip 은 별도 책임(display 계층).
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of(f"/data/{_UUID}__씨름.mp4"), f"{_UUID}__씨름.mp4")

    def test_query_only_fallback(self) -> None:
        # 스킴/쿼리만 남고 파일명이 비면 tail 로 폴백(기존 3벌 공통 `or tail`).
        from src.config.filename_util import basename_of

        self.assertEqual(basename_of("?onlyquery"), "?onlyquery")


class TestNoResidualBasenameDefs(unittest.TestCase):
    """RED ②: 각 모듈에 ``_basename`` 정의가 남지 않는다(공개 basename_of 1벌 대체).

    069 T407: 3번째 대상 ``sample_search_api`` 는 삭제됨(basename_of 소비처가 함께 소멸) →
    잔여 2 모듈(search_group·opensearch_sync)만 확인한다.
    """

    def test_modules_have_no_private_basename(self) -> None:
        import service.portal.search_group as group
        import src.search.opensearch_sync as ossync

        for mod in (group, ossync):
            self.assertFalse(
                hasattr(mod, "_basename"),
                f"{mod.__name__} 에 _basename 잔존 — basename_of 로 통합해야 함",
            )


class TestCallSiteOutputsPreserved(unittest.TestCase):
    """각 호출처의 관찰 출력이 통합 전후 동일해야 한다(이 그룹의 최우선)."""

    def test_search_group_display_strips_prefix(self) -> None:
        # search_group 표시명 = basename_of + asset_id 프리픽스 제거(065 T605) — strip 유지.
        from service.portal.search_group import group_ranked

        result = {
            "query": "q",
            "results": {
                "video": [
                    {
                        "id": "v1",
                        "similarity": 0.5,
                        "file_uri": f"/data/{_UUID}__씨름.mp4",
                        "summary": "s",
                    }
                ]
            },
            "meta": {},
        }
        grouped = group_ranked(result, limit_per_modality=10)
        self.assertEqual(grouped["video"][0]["file_name"], "씨름.mp4")

    def test_search_group_display_non_prefixed_unchanged(self) -> None:
        from service.portal.search_group import group_ranked

        result = {
            "query": "q",
            "results": {
                "text_documents": [
                    {
                        "id": "t1",
                        "similarity": 0.5,
                        "file_uri": "/data/wikipedia_커피_9204.txt",
                        "summary": "s",
                    }
                ]
            },
            "meta": {},
        }
        grouped = group_ranked(result, limit_per_modality=10)
        self.assertEqual(grouped["text"][0]["file_name"], "wikipedia_커피_9204.txt")

    # 069 T407: test_sample_compact_view_does_not_strip_prefix 제거 — sample_search_api 삭제.
    # 포탈로 이관된 compact 뷰는 이미 group_ranked 로 display_name(프리픽스 제거)된 grouped 를
    # 소비하므로 프리픽스를 벗긴다(포탈 표준 표시명·의료 배제 보존). sample 의 원시 basename(미제거)
    # 동작은 디버그 도구의 부수적 특성이었고 재현하지 않는다(정당성: 069 T407 보고).

    def test_opensearch_sync_indexes_raw_basename_no_strip(self) -> None:
        # opensearch_sync file_name 은 basename_of(프리픽스 미제거) → clean_file_name 정제 결과.
        # 프리픽스의 UUID 토큰은 clean_file_name 이 ID 로 제거하고 원본 파일명 토큰만 남는다.
        from src.search.opensearch_sync import asset_to_doc

        row = {
            "asset_id": "A1",
            "modality": "text",
            "domain_label": "general",
            "fs_path": f"/data/{_UUID}__무선_충전기_xyz.mp4",
            "created_at": None,
            "ext_meta": {"summary": "s", "keywords": [], "labels": []},
            "emb": "[0.1,0.2,0.3]",
        }
        doc = asset_to_doc(row, channel="st")
        # fs_uri 는 원본 경로 그대로(프리픽스 포함), file_name 은 basename_of 후 clean.
        self.assertEqual(doc["fs_uri"], f"/data/{_UUID}__무선_충전기_xyz.mp4")
        self.assertEqual(doc["file_name"], "무선 충전기 xyz")


if __name__ == "__main__":
    unittest.main()
