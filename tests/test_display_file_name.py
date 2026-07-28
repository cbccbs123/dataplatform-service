"""065 T605 — 표시용 파일명 asset_id 프리픽스 제거 단위 테스트 (FR-705).

archiver 가 registered 자산을 ``{asset_id}__{원본명}`` 으로 아카이브하며 fs_path 에 프리픽스가
붙는다. 프론트 트리·자산 목록·다운로드가 그 basename 을 그대로 보여주면 asset_id 가 노출되므로,
표시 경로에서 프리픽스만 벗긴다. 정본 헬퍼(archiver)와 순수모듈 사본(search_group)을 함께 검증한다.
"""

from __future__ import annotations

import unittest

from service.portal.search_group import display_name
from src.config.filename_util import display_file_name, strip_asset_id_prefix

_UUID = "018f0000-0000-7000-8000-000000000271"


class TestStripAssetIdPrefix(unittest.TestCase):
    def test_strips_uuid_prefix(self) -> None:
        self.assertEqual(
            strip_asset_id_prefix(f"{_UUID}__24시간 굶고_(라면).mp4"),
            "24시간 굶고_(라면).mp4",
        )

    def test_keeps_non_prefixed(self) -> None:
        # 인입 원본(프리픽스 없음)은 그대로.
        self.assertEqual(strip_asset_id_prefix("wikipedia_커피_9204.txt"), "wikipedia_커피_9204.txt")

    def test_keeps_double_underscore_without_uuid(self) -> None:
        # 맨 앞이 UUID 형태가 아니면 원본에 '__' 가 있어도 안 건드림.
        self.assertEqual(strip_asset_id_prefix("a__b__c.txt"), "a__b__c.txt")

    def test_empty(self) -> None:
        self.assertEqual(strip_asset_id_prefix(""), "")


class TestDisplayFileName(unittest.TestCase):
    def test_archive_path_prefix_stripped(self) -> None:
        p = f"/opt/airflow/archive/20260708/{_UUID}__Gimbap_(pixabay)_(김밥).jpg"
        self.assertEqual(display_file_name(p), "Gimbap_(pixabay)_(김밥).jpg")

    def test_inbox_path_unchanged(self) -> None:
        p = "/opt/airflow/inbox/wikipedia_산호초_515290.txt"
        self.assertEqual(display_file_name(p), "wikipedia_산호초_515290.txt")

    def test_none_and_empty(self) -> None:
        self.assertEqual(display_file_name(None), "")
        self.assertEqual(display_file_name(""), "")


class TestSearchGroupBasenameStrips(unittest.TestCase):
    def test_basename_strips_prefix(self) -> None:
        # 표시명 헬퍼도 동일하게 프리픽스를 벗긴다(계약 일치·069 D3 후 display_name).
        uri = f"/data/{_UUID}__씨름 한판_(씨름).mp4"
        self.assertEqual(display_name(uri), "씨름 한판_(씨름).mp4")

    def test_basename_non_prefixed(self) -> None:
        self.assertEqual(display_name("/data/wikipedia_커피_9204.txt"), "wikipedia_커피_9204.txt")


if __name__ == "__main__":
    unittest.main()
