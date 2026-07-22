"""검색 모달리티 단일 출처(T301·D5·P2-29) 단위 테스트 — DB·네트워크 없음.

리뷰 P2-29: 유효 모달리티 튜플과 CSV 파싱이 여러 진입점에 복제돼 있던 것을 코어
``src.config.search_modalities`` **단일 출처**로 모았다. 이 백엔드 테스트는 (a) 공유 파서
``parse_modalities_csv`` 의 순수 파싱 계약, (b) 백엔드 검색 진입점(``service.api.routes_search``)이
자체 튜플을 복제하지 않고 코어 공유 상수를 **참조**함(객체 동일성)을 봉인한다.

※ 레포 분리: 코어/파이프라인 진입점(run_search 등)의 동일성 및 ``src/`` 전역 단일출처 grep 가드는
  각 레포가 소유한다(백엔드 트리엔 ``src/`` 가 없다 — 코어는 site-packages 로 설치). 여기선 백엔드 몫만.
"""
from __future__ import annotations

import unittest

from src.config.search_modalities import VALID_SEARCH_MODALITIES, parse_modalities_csv


class TestParseModalitiesCsv(unittest.TestCase):
    """공유 파서 ``parse_modalities_csv`` — split/strip·미지정=None·검증/소문자화 없음(RED ①)."""

    def test_none_and_blank_return_none(self) -> None:
        # 미지정/공백 → None(전체 버킷). 3진입점 공통 계약.
        self.assertIsNone(parse_modalities_csv(None))
        self.assertIsNone(parse_modalities_csv(""))
        self.assertIsNone(parse_modalities_csv("   "))

    def test_comma_split_and_strip(self) -> None:
        # 콤마 분리 + 공백 트림 + 빈 토큰 스킵(기존 3진입점 동일 동작 보존).
        self.assertEqual(parse_modalities_csv("text,image"), ["text", "image"])
        self.assertEqual(parse_modalities_csv(" text , , image "), ["text", "image"])

    def test_case_preserved_not_lowercased(self) -> None:
        # 동작 불변(US-D 원칙): 원본 3파서가 소문자화하지 않았으므로 여기서도 하지 않는다.
        # 대문자/혼합 입력은 원문 그대로 유지 → 이후 유효값(소문자 튜플) 밖으로 거부된다.
        self.assertEqual(parse_modalities_csv("TEXT,Image,VIDEO"), ["TEXT", "Image", "VIDEO"])

    def test_all_blank_returns_none(self) -> None:
        # 콤마만/공백만 → None(빈 리스트가 아니라 None = 전체 버킷).
        self.assertIsNone(parse_modalities_csv(" , , "))


class TestValidModalitiesSingleSource(unittest.TestCase):
    """유효값 튜플이 코드에 1벌만 — 3진입점이 공유 상수를 참조(RED ②)."""

    def test_valid_tuple_value(self) -> None:
        self.assertEqual(VALID_SEARCH_MODALITIES, ("text", "image", "video", "audio"))

    def test_entrypoints_reference_shared_constant(self) -> None:
        # 백엔드 검색 진입점(routes_search)이 자체 튜플을 복제하지 않고 코어 공유 상수를 참조(객체 동일성).
        # (레포 분리: run_search 등 코어·파이프라인 진입점의 동일성 검증은 각 레포가 소유 — 여기선 백엔드 몫만.)
        from service.api import routes_search as portal

        self.assertIs(portal.VALID_SEARCH_MODALITIES, VALID_SEARCH_MODALITIES)

    # (레포 분리로 제거) test_valid_tuple_literal_defined_once — `= ("text","image","video","audio")`
    #   리터럴이 ``src/`` 전역에서 1곳뿐인지 검사하는 grep 가드는 **코어 단일출처 불변식**이라 코어 레포가
    #   소유한다. 백엔드 트리엔 ``src/`` 가 없어(코어는 설치본) 항상 빈 결과 → 여기서 검증하지 않는다.


# 069 T407: TestSampleSearchModalityContract(sample 미지 모달리티 200+{"error"} 보존)를 제거했다 —
# sample_search_api 삭제로 그 응답 계약이 소멸(CR-13 moot). 미지 모달리티 거부는 이제 포탈
# _parse_modalities 의 HTTPException(400)이 담당
# (tests.test_portal_api.test_search_unknown_modality_400).


if __name__ == "__main__":
    unittest.main()
