"""HTTP Range 헤더 파싱(``parse_range_header``) 순수 단위 테스트.

DB·네트워크·파일 IO 없이 완전 순수. 경계/이상 입력(전체·접미·열린끝·초과·역순·형식오류)을
충실히 덮는다. 범위 위반은 ``ValueError`` 로 거부(API 가 416 으로 매핑, plan 010 D-4).
"""
from __future__ import annotations

import unittest


class TestParseRangeHeader(unittest.TestCase):
    def test_none_header_means_full(self) -> None:
        # Range 헤더 없음 → None(전체 다운로드).
        from service.portal.download import parse_range_header

        self.assertIsNone(parse_range_header(None, 1000))

    def test_explicit_start_end(self) -> None:
        # bytes=0-99 → (0, 99).
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=0-99", 1000), (0, 99))

    def test_open_ended_start(self) -> None:
        # bytes=100- → (100, size-1) (열린 끝 = 파일 끝까지).
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=100-", 1000), (100, 999))

    def test_suffix_range(self) -> None:
        # bytes=-50 → (size-50, size-1) (마지막 50바이트).
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=-50", 1000), (950, 999))

    def test_suffix_larger_than_file_clamps_to_whole(self) -> None:
        # 접미 길이가 파일보다 크면 전체로 클램프(RFC 7233) → (0, size-1). 에러 아님.
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=-5000", 1000), (0, 999))

    def test_full_file_range(self) -> None:
        # bytes=0-999 (정확히 파일 끝) → (0, 999), 경계 허용.
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=0-999", 1000), (0, 999))

    def test_whitespace_tolerated(self) -> None:
        # 헤더 주변 공백은 허용(관용적 파싱).
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("  bytes=0-99  ", 1000), (0, 99))

    def test_start_beyond_file_raises(self) -> None:
        # 시작이 파일 크기 이상 → 만족 불가(416).
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("bytes=1000-1100", 1000)

    def test_end_beyond_file_clamps(self) -> None:
        # 끝이 파일 크기 이상이면 file_size-1 로 클램프(RFC 7233 §2.1) → 거부 아님.
        # 일부 미디어 플레이어/다운로드 매니저가 stale 한 큰 end 를 재요청해도 206 으로 응답.
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=0-1000", 1000), (0, 999))

    def test_partial_start_with_end_beyond_clamps(self) -> None:
        # bytes=500-5000 (start 유효, end 초과) → (500, 999) 로 클램프(부분 범위 유지).
        from service.portal.download import parse_range_header

        self.assertEqual(parse_range_header("bytes=500-5000", 1000), (500, 999))

    def test_reversed_range_raises(self) -> None:
        # 역순(start > end) → ValueError.
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("bytes=500-100", 1000)

    def test_missing_bytes_unit_raises(self) -> None:
        # 'bytes=' 접두 없음 → 형식 오류.
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("items=0-99", 1000)

    def test_non_numeric_raises(self) -> None:
        # 숫자가 아닌 값 → 형식 오류.
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("bytes=a-z", 1000)

    def test_empty_spec_raises(self) -> None:
        # 'bytes=-' (시작·접미 모두 없음) → 형식 오류.
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("bytes=-", 1000)

    def test_multi_range_unsupported(self) -> None:
        # 다중 범위(콤마)는 본 MVP 미지원 → ValueError(단일 범위만).
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("bytes=0-10,20-30", 1000)

    def test_zero_byte_file_raises(self) -> None:
        # 빈 파일(size=0)에 대한 모든 범위는 만족 불가 → ValueError(경계).
        from service.portal.download import parse_range_header

        with self.assertRaises(ValueError):
            parse_range_header("bytes=0-0", 0)


if __name__ == "__main__":
    unittest.main()
