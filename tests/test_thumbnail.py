"""자산 썸네일 생성 단위 테스트 (057-후속) — PIL 로 소형 이미지를 만들어 순수 검증(실 OS·DB 불필요).

검증 의도
    - 이미지 → JPEG 바이트, 최대 변 ``THUMB_MAX_DIM`` 이내(비율 보존).
    - 결정성(헌법 3조): 동일 입력 → 동일 바이트.
    - 비대상 modality(audio/text/unknown)·빈/부재 경로 → ``None``(엔드포인트 404 → 프론트 아이콘).
    (영상 경로는 실 영상 파일이 필요해 여기선 modality 게이트만; 실값은 엔드포인트/실DB 게이트에서.)
"""
from __future__ import annotations

import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from service.portal.thumbnail import THUMB_MAX_DIM, cached_thumbnail, generate_thumbnail


def _make_png(path: str, size: tuple[int, int] = (800, 600)) -> None:
    from PIL import Image

    Image.new("RGB", size, (120, 60, 200)).save(path, "PNG")


class GenerateThumbnailTest(unittest.TestCase):
    def test_image_returns_jpeg_within_max_dim_ratio_preserved(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.png")
            _make_png(p, (800, 600))
            data = generate_thumbnail(p, "image")
            self.assertIsNotNone(data)
            im = Image.open(BytesIO(data))
            self.assertEqual(im.format, "JPEG")
            self.assertLessEqual(max(im.size), THUMB_MAX_DIM)
            self.assertEqual(im.size, (THUMB_MAX_DIM, 240))  # 800x600 → 320x240(비율 보존)

    def test_deterministic_same_bytes(self) -> None:
        # 동일 입력 → 동일 바이트(결정성·헌법 3조). 고정 리샘플(LANCZOS)·JPEG 파라미터.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.png")
            _make_png(p, (640, 480))
            self.assertEqual(generate_thumbnail(p, "image"), generate_thumbnail(p, "image"))

    def test_small_image_not_upscaled(self) -> None:
        # 원본이 상한보다 작으면 확대하지 않는다(thumbnail 은 축소 전용).
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.png")
            _make_png(p, (100, 80))
            im = Image.open(BytesIO(generate_thumbnail(p, "image")))
            self.assertEqual(im.size, (100, 80))

    def test_max_dim_controls_output_size(self) -> None:
        # 057-후속: max_dim 인자로 detail(640) 등 더 큰 히어로 썸네일 생성(비율 보존).
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "big.png")
            _make_png(p, (800, 600))
            im = Image.open(BytesIO(generate_thumbnail(p, "image", max_dim=640)))
            self.assertEqual(im.size, (640, 480))  # 800x600 → 640x480(detail)

    def test_non_thumbnailable_modality_none(self) -> None:
        self.assertIsNone(generate_thumbnail("/x/a.txt", "text"))
        self.assertIsNone(generate_thumbnail("/x/a.mp3", "audio"))
        self.assertIsNone(generate_thumbnail("/x/a.bin", "unknown"))
        self.assertIsNone(generate_thumbnail("/x/a.png", None))

    def test_missing_or_empty_path_none(self) -> None:
        self.assertIsNone(generate_thumbnail("", "image"))
        self.assertIsNone(generate_thumbnail("/nonexistent/nope.png", "image"))  # open 실패 → 격리 None


class CachedThumbnailTest(unittest.TestCase):
    """디스크 캐시 경유 — generate-once·캐시 히트·경로조작 방지·None 미캐시(057-후속)."""

    def test_generate_once_then_cache_hit(self) -> None:
        # 첫 호출 생성·저장, 둘째 호출은 캐시 히트(재생성 0). generate_thumbnail 호출 횟수로 검증.
        with tempfile.TemporaryDirectory() as d:
            with patch("service.portal.thumbnail.generate_thumbnail", return_value=b"JPGDATA") as gen:
                b1 = cached_thumbnail("asset-1", "/x/a.png", "image", cache_dir=d)
                b2 = cached_thumbnail("asset-1", "/x/a.png", "image", cache_dir=d)
            self.assertEqual(b1, b"JPGDATA")
            self.assertEqual(b2, b"JPGDATA")
            gen.assert_called_once()  # 둘째는 원본 안 읽고 캐시 서빙
            self.assertTrue(os.path.isfile(os.path.join(d, "asset-1_320.jpg")))  # 기본 card=320

    def test_size_detail_separate_cache_and_dim(self) -> None:
        # 057-후속: size=detail 은 640 으로 별도 캐시 키(_640)·generate 에 max_dim=640 전달.
        with tempfile.TemporaryDirectory() as d:
            with patch("service.portal.thumbnail.generate_thumbnail", return_value=b"HERO") as gen:
                out = cached_thumbnail("asset-9", "/x/a.png", "image", size="detail", cache_dir=d)
            self.assertEqual(out, b"HERO")
            gen.assert_called_once_with("/x/a.png", "image", max_dim=640)
            self.assertTrue(os.path.isfile(os.path.join(d, "asset-9_640.jpg")))

    def test_unknown_size_falls_back_to_card(self) -> None:
        # 미지원 size 는 card(320) 폴백 — 타이포에 404 대신 기본 서빙(장식적 자원).
        with tempfile.TemporaryDirectory() as d:
            with patch("service.portal.thumbnail.generate_thumbnail", return_value=b"J") as gen:
                cached_thumbnail("asset-x", "/x/a.png", "image", size="huge", cache_dir=d)
            gen.assert_called_once_with("/x/a.png", "image", max_dim=320)
            self.assertTrue(os.path.isfile(os.path.join(d, "asset-x_320.jpg")))

    def test_generate_none_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with patch("service.portal.thumbnail.generate_thumbnail", return_value=None):
                self.assertIsNone(cached_thumbnail("asset-2", "/x/a.png", "image", cache_dir=d))
            self.assertFalse(os.path.isfile(os.path.join(d, "asset-2_320.jpg")))  # 실패는 캐시 안 함

    def test_unsafe_asset_id_skips_cache(self) -> None:
        # 경로 조작 방지 — 안전 패턴 아니면 캐시 파일 생성 없이 직접 생성.
        with tempfile.TemporaryDirectory() as d:
            with patch("service.portal.thumbnail.generate_thumbnail", return_value=b"J") as gen:
                out = cached_thumbnail("../etc/passwd", "/x/a.png", "image", cache_dir=d)
            self.assertEqual(out, b"J")
            gen.assert_called_once()
            self.assertEqual(os.listdir(d), [])  # 캐시 파일 안 만듦

    def test_non_thumbnailable_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(cached_thumbnail("a", "/x/a.mp3", "audio", cache_dir=d))


if __name__ == "__main__":
    unittest.main()
