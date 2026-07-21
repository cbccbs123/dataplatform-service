"""자산 썸네일 생성 (057-후속) — 이미지·영상 시각 미리보기.

원본(``fs_path``)에서 축소 썸네일 JPEG 바이트를 만든다. **읽기 전용**(원본·DB 무수정, 헌법 6조)·
**결정적**(같은 파일 → 같은 바이트, 헌법 3조)·**학습/LLM 0**(단순 이미지 리사이즈·프레임 추출·헌법 1·2조).

- **이미지**: PIL 로 열어 EXIF 회전 반영 후 요청 크기(card 320·detail 640)의 최대 변으로 축소.
- **영상**: cv2 로 **대표 프레임 1개**(1초 지점·검은 첫 프레임 회피·결정적) 추출 후 동일 축소.
- **오디오/텍스트/unknown**: 시각 표현이 없어 ``None`` → 엔드포인트 404 → 프론트가 모달리티 아이콘 폴백.

의존(cv2·PIL)은 **함수 내부 지연 import**(모듈 순수성 — 미사용 환경 import 부담 0). **의료(PHI) 배제는
엔드포인트 게이트**(``resolve_download_target`` registered·비의료)가 담당하므로 여기선 modality 만 본다.
디스크 캐시(``cached_thumbnail``)로 크기별 generate-once 하고 브라우저 ``Cache-Control`` 로 완화한다.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("meta_extract.thumbnail")

# 057-후속: 용도별 크기 프리셋 — card(목록·hover)·detail(상세 히어로). _MAX_DIM 캡으로 남용 방지.
_SIZE_PRESETS = {"card": 320, "detail": 640}
_MAX_DIM = 1024
THUMB_MAX_DIM = _SIZE_PRESETS["card"]  # 기본(card) 최대 변(px) — 프리셋 단일 출처(중복 리터럴 드리프트 방지)
THUMBNAILABLE_MODALITIES = frozenset({"image", "video"})
_VIDEO_POS_MSEC = 1000.0  # 대표 프레임 위치(1초) — 0초는 검은 프레임이 흔해 회피(결정적)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")  # 캐시 파일명 안전 id(경로 조작 방지)


def _resolve_dim(size: str | None) -> int:
    """크기 프리셋 → 최대 변(px). 미지정/미지원은 card(기본)·최대 ``_MAX_DIM`` 캡(남용 방지)."""
    return min(_SIZE_PRESETS.get(size or "card", THUMB_MAX_DIM), _MAX_DIM)


def _encode_thumb(pil_img: Any, max_dim: int = THUMB_MAX_DIM) -> bytes:
    """PIL 이미지 → 축소 JPEG 바이트(순수·결정적). EXIF 회전 반영·RGB·LANCZOS 리샘플."""
    from io import BytesIO

    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(pil_img) or pil_img
    img = img.convert("RGB")
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


def generate_thumbnail(
    fs_path: str | None, modality: str | None, *, max_dim: int = THUMB_MAX_DIM
) -> bytes | None:
    """원본 ``fs_path`` → 최대 변 ``max_dim`` 축소 JPEG 바이트(결정적). 비대상/실패/손상 → ``None``.

    어떤 예외도 전파하지 않는다(손상 파일·미지원 코덱 등은 썸네일 없음=404 로 격리). 결정성:
    이미지 리사이즈·영상 고정 위치 프레임·고정 JPEG 파라미터라 동일 입력 → 동일 출력.
    """
    if modality not in THUMBNAILABLE_MODALITIES or not fs_path:
        return None
    try:
        if modality == "image":
            from PIL import Image

            with Image.open(fs_path) as im:
                return _encode_thumb(im, max_dim)
        # video: cv2 로 대표 프레임 1개(1초 지점·실패 시 첫 프레임)
        import cv2
        from PIL import Image

        cap = cv2.VideoCapture(fs_path)
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, _VIDEO_POS_MSEC)
            ok, frame = cap.read()
            if not ok or frame is None:  # 1초 지점 실패 → 첫 프레임 폴백
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok or frame is None:
                return None
        finally:
            cap.release()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return _encode_thumb(Image.fromarray(rgb), max_dim)
    except Exception as exc:  # noqa: BLE001 — 손상 파일·코덱 등은 썸네일 없음(404)으로 격리(best-effort)
        _LOG.warning("썸네일 생성 실패(무시): fs_path=%s modality=%s: %s", fs_path, modality, exc)
        return None


def _default_cache_dir() -> Path:
    """썸네일 디스크 캐시 경로 — ``THUMBNAIL_CACHE_DIR`` 환경변수, 없으면 temp 하위(파생·삭제 안전)."""
    return Path(os.getenv("THUMBNAIL_CACHE_DIR") or (Path(tempfile.gettempdir()) / "dataflatform_thumb_cache"))


def cached_thumbnail(
    asset_id: str,
    fs_path: str | None,
    modality: str | None,
    *,
    size: str = "card",
    cache_dir: str | Path | None = None,
) -> bytes | None:
    """디스크 캐시 경유 썸네일(**generate-once**) — 있으면 캐시 서빙, 없으면 1회 생성→저장→반환.

    캐시 키 = ``<asset_id>_<dim>.jpg`` (``dim`` = ``size`` 프리셋의 최대 변). **크기별로 캐시가 분리**되어
    card(320)·detail(640) 각각 generate-once 된다. 두 번째 요청부터는 원본을 읽지 않고 캐시 파일만
    서빙한다(영상 재디코드 0). 결정적이라 동시 생성돼도 바이트 동일 — 임시파일 후 ``os.replace`` 원자
    교체로 부분읽기 방지. 캐시는 **파생물**(원본·DB 무수정, 헌법 6조)이라 삭제·재생성 안전. 비대상
    modality/생성 실패는 ``None``(캐시하지 않음). ``asset_id`` 가 안전 패턴이 아니면 경로 조작 방지로
    캐시를 건너뛰고 직접 생성.

    ⚠️ 캐시 기능은 자체 완결적이다 — 스키마/파이프라인 변경 0. 제거 시 이 함수·엔드포인트·캐시 디렉터리만
    치우면 되고(마이그레이션·데이터 모델 없음), ``generate_thumbnail`` 로 즉시 되돌아간다.
    """
    if modality not in THUMBNAILABLE_MODALITIES or not fs_path:
        return None
    dim = _resolve_dim(size)
    if not _SAFE_ID_RE.match(asset_id or ""):  # 경로 조작 방지 — 캐시 스킵하고 직접 생성
        return generate_thumbnail(fs_path, modality, max_dim=dim)
    cdir = Path(cache_dir) if cache_dir is not None else _default_cache_dir()
    cpath = cdir / f"{asset_id}_{dim}.jpg"
    try:
        if cpath.is_file():
            return cpath.read_bytes()  # 캐시 히트 — 원본 안 읽음
    except OSError as exc:
        _LOG.warning("썸네일 캐시 읽기 실패(재생성): %s: %s", cpath, exc)
    data = generate_thumbnail(fs_path, modality, max_dim=dim)
    if data is None:
        return None
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        # 고유 임시파일(mkstemp) → 원자 교체: 동일 워커 내 동시 스레드도 tmp 경로가 겹치지 않는다
        # (PID-only 는 스레드 충돌 여지). 실패 시 tmp 잔여 제거 후 전파(바깥 except 가 best-effort 처리).
        fd, tmp_name = tempfile.mkstemp(dir=cdir, prefix=f"{asset_id}_{dim}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, cpath)  # 원자 교체(부분읽기 방지)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:  # 저장 실패해도 생성분은 반환(캐시는 best-effort)
        _LOG.warning("썸네일 캐시 저장 실패(무시): %s: %s", cpath, exc)
    return data
