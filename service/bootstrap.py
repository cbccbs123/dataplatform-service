"""백엔드 부트스트랩 — dataplatform-service 자기 레포 루트의 ``.env.{env}`` 로드 + 코어 init_settings.

왜 코어의 ``src.config.bootstrap.bootstrap_env`` 를 그대로 쓰지 않는가(비자명):
    코어 부트스트랩은 ``bootstrap.py`` 파일 위치(코어 레포) 기준으로 ``.env.{env}`` 를 찾는다 — 코어 레포
    자체 진입점(run_ingest 등)용이다. 백엔드가 그걸 호출하면 **코어 레포의 .env 를 로드**해 버려, 백엔드
    자신의 설정(``dataplatform-service/.env.{env}``)이 무시된다. 그래서 백엔드는 **자기 레포 루트** 기준으로
    .env 를 로드하는 전용 부트스트랩을 둔다. 로드 후엔 **코어의** ``init_settings`` 로 필수 env 검증 +
    frozen 설정 생성(이후 ``get_current_settings`` 활성) — 설정 스키마·검증 로직은 코어 단일 출처를 재사용한다.

    (코어는 ``src.*`` 로 설치해 참조하고, 백엔드 코드는 ``service.*`` 다.)
"""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# 코어(설치/참조)의 설정 생성 로직 재사용 — 스키마·필수 env 검증 단일 출처.
from src.config.settings import PipelineSettings, init_settings

# service/bootstrap.py → parents[1] = 백엔드 레포 루트(dataplatform-service). 코어가 아니라 **여기** 기준.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def bootstrap_env(env: str) -> PipelineSettings:
    """``dataplatform-service/.env.{env}`` 로드(있으면·override=False) 후 코어 ``init_settings(env)``.

    ``override=False``: OS 에 이미 있는 환경변수(컨테이너 주입 등)를 .env 가 덮지 않는다. .env 부재 시
    (환경변수 직접 주입) 로드는 건너뛰고 init_settings 는 그대로 수행한다(코어 bootstrap 동형)."""
    env_file = _REPO_ROOT / f".env.{env}"
    if env_file.exists():
        load_dotenv(env_file, override=False)
    return init_settings(env)
