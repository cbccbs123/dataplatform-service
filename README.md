# dataplatform-service

멀티모달 데이터 통합 플랫폼의 **HTTP API**입니다. 하이브리드 검색·자산 상세·다운로드·썸네일·
관계 검토 서빙을 제공합니다.

> 국책과제 **RS-2025-02215256** 산출물.

## 세 레포의 관계

| 레포 | 파이썬 패키지 | 역할 |
|---|---|---|
| dataplatform-core | `src.*` | 규약·계약·순수 로직 + DB 스키마 정본 |
| dataplatform-pipeline | `processing.*` | 처리 파이프라인(Airflow) |
| **dataplatform-service**(이 레포) | `service.*` | HTTP API |

**이 레포는 코어를 필요로 합니다.** 코어가 없으면 `service.*` 이 import 되지 않습니다.

## 설계 — 2계층

| 계층 | 위치 | 책임 |
|---|---|---|
| 전송 | `service/api/` | FastAPI 라우팅·요청/응답 모델·미들웨어·예외 처리 |
| 로직 | `service/portal/` | 검색 조립·자산 조회·다운로드·썸네일·인증·권한 |

검색·관계 조회·임베딩 같은 도메인 로직은 코어(`src.*`)에 위임합니다. 이 레포는 **서빙**만 합니다.

## 요구사항

| 항목 | 버전 |
|---|---|
| Python | **3.13 이상** |
| PostgreSQL | 17 + `pgvector` (코어 스키마) |
| OpenSearch | `analysis-nori` 플러그인 |

## 설치

**코어를 먼저 설치합니다.**

```bash
# ① 코어 — 나란히 clone 해서 참조형으로 설치(개발) 또는 태그로 설치형
git clone https://github.com/OWNER/dataplatform-core.git
pip install -e ./dataplatform-core

# ② 이 레포
pip install -e .
```

코어를 설치하면 psycopg·opensearch-py 등이 전이로 따라옵니다. 이 레포의 `pyproject.toml` 에는
코어가 제공하지 않는 것(fastapi·uvicorn·PyJWT·pydantic·opencv)만 있습니다.

## 환경변수

레포 루트에 `.env.dev` 를 두면 부트스트랩이 읽습니다. 필요한 변수 **이름만** 적습니다.

| 구분 | 변수 |
|---|---|
| 프로파일 | `PORTAL_API_ENV`(기본 `dev`) |
| DB | `POSTGRES_HOST` · `POSTGRES_PORT` · `POSTGRES_DB` · `POSTGRES_USER` · `POSTGRES_PASSWORD` |
| 검색 | `OPENSEARCH_HOST` · `OPENSEARCH_PORT` |
| 인증 | `PORTAL_AUTH_DISABLED` · `PORTAL_JWT_SECRET` · `PORTAL_JWT_ISSUER` · `PORTAL_JWT_TTL_SECONDS` · `PORTAL_AUTH_BACKEND` |
| 파일 | `WATCHER_ARCHIVE_DIR`(다운로드·썸네일이 읽는 보관 경로) |

> ⚠️ 보관 경로는 **파이프라인 레포와 같은 값**이어야 합니다(같은 파일을 읽습니다).

### 인증 동작 (중요)

| `PORTAL_AUTH_DISABLED` | 동작 |
|---|---|
| `1` (연구·개발) | 토큰 없이 호출 가능 → `anonymous`(public 권한). Bearer 가 있으면 검증합니다. `POST /auth/token` 으로 dev 토큰 발급 가능 |
| `0` (운영) | Bearer **필수**(없으면 401) · `POST /auth/token` 은 404 · **`PORTAL_JWT_SECRET` 미설정이면 기동 시점에 실패**(fail-fast) |

JWT 는 HS256 이고 `exp`·`sub` 를 필수로 검증합니다. `PORTAL_JWT_ISSUER` 를 설정하면 `iss` 를 고정해
다른 서비스의 토큰 재사용을 막습니다.

## 실행

```bash
uvicorn service.api:app --host 127.0.0.1 --port 8001
# 또는
./run.sh start        # pid·로그 관리 · 중복 기동 방지 · 기동 직후 생존 확인
./run.sh status       # 중지: stop · 재기동: restart · 포트 변경: BACKEND_PORT=8000 ./run.sh start
```

> ⚠️ `--host 0.0.0.0` 은 모든 네트워크 인터페이스에 노출됩니다. 신뢰된 네트워크가 아니면
> 리버스 프록시 뒤에 두고 인증을 활성화(`PORTAL_AUTH_DISABLED=0`)하십시오.

전제: 코어에서 **DB 스키마 생성 + 닫힌 taxonomy 시드**가 끝나 있어야 하고, 파이프라인이 자산을
적재·색인해 둔 상태여야 검색 결과가 나옵니다.

## 확인

```bash
curl "http://127.0.0.1:8001/health"
curl "http://127.0.0.1:8001/search?q=김치%20담그기&modalities=video,text&size=10"
```

`GET /search` 는 결과를 **모달리티별 그룹**(text·image·video·audio)으로 반환하며 섹션마다 독립
랭킹입니다. 주제·기간·확장자·출처 필터와 주제 facet 을 함께 제공합니다.

## 테스트

```bash
python -m unittest discover -s tests
```

## 구조

```
service/
  api/          FastAPI 앱·라우터·요청/응답 모델·미들웨어
  portal/       검색 조립·자산 조회·다운로드·썸네일·인증/권한
  bootstrap.py  설정 로드·초기화
tests/          단위 테스트
run.sh          네이티브 실행 제어(start|stop|restart|status)
```

## 설계 제약

- **학습 기반 방식을 쓰지 않습니다** — 사전학습 모델은 추론 전용입니다.
- 배포는 네이티브입니다(Dockerfile·compose 를 두지 않습니다). 공유 스토리지는 호스트 파일시스템 경로로 접근합니다.
- 코드·주석·로그는 한국어로 작성합니다.
