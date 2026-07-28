"""검색 라우트 — 검색 실행 + 권한별 필드 가리기 + 주제 패싯 집계.

**흐름에서의 위치**: 요청을 코어 검색 함수에 넘기고, 돌아온 결과를 **요청자 권한에 맞게**
가린 뒤 화면이 쓸 패싯까지 붙여 응답한다. 검색 알고리즘 자체는 코어에 있다.

**권한 투영은 응답 직전에 한다** — 색인이나 검색 자체를 건드리지 않는다. 그래야 권한 정책이
바뀌어도 색인을 다시 만들 필요가 없다.

인프라 함수는 ``from ... import`` 가 아니라 **모듈 경유**로 쓴다 — 그래야 테스트가 이 모듈의
이름을 갈아끼워 DB 없이 검증할 수 있다.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import _infra
from service.portal.auth import Principal, require_principal
from service.portal.search_group import group_ranked
from src.config.search_modalities import VALID_SEARCH_MODALITIES, parse_modalities_csv
from src.registry.access_tier import project_ext_meta
from src.registry.ext_meta_field_registry import fetch_access_tiers
from src.search.search_filters import parse_search_filters
from src.search.search_service import search_hybrid

router = APIRouter()

# 배제할 도메인 목록. **지금은 비어 있다**(모든 도메인을 균일하게 노출) — 특정 도메인을
# 다시 가려야 할 때 여기에 넣으면 결과 조립 단계가 그 행들을 걷어낸다.
# 의료 특수 트랙 미운용. 의료 복귀(3년차) 시 frozenset({"medical"}) 로 되돌린다.
_EXCLUDE_DOMAINS: frozenset[str] = frozenset()

# search_hybrid 의 버킷당 후보 풀 **기본값**. /search 의 limit_per_bucket 로 요청마다 덮어쓴다.
# 응답은 모달리티별 상위 N개만 내보내지만, 후보 풀은 그보다 깊게 받아야 한다 — (a)배제된 행
# 드롭 후 승격 여지가 생긴다 — 핸들러가 max(풀, size)로 하한을 걸어 풀<size 회귀를 막는다.
_SEARCH_LIMIT_PER_BUCKET_DEFAULT = 50
# 풀 상한(요청 남용·OS 부하 방어). size 상한(100)보다 넉넉히 둬 승격 여지를 남긴다.
_SEARCH_LIMIT_PER_BUCKET_MAX = 500

# 간략 보기에서 요약을 자를 길이(고정값 — 요청 파라미터로 받지 않는다).
_COMPACT_SUMMARY_CHARS = 160


def _project_grouped_search(
    conn: Any,
    grouped: dict[str, list[dict[str, Any]]],
    *,
    clearance: str,
) -> dict[str, list[dict[str, Any]]]:
    """검색 결과의 요약에서 **권한이 못 보는 항목을 지운다**.

    권한이 못 미치면 그 키를 **행에서 아예 뺀다**(빈 값으로 바꾸지 않는다 — 키의 존재 자체가
    '요약이 있다'는 정보이기 때문). 색인은 건드리지 않고 응답 단계에서만 가린다.

    Args:
        grouped: 모달리티별 결과 행. 원본을 바꾸지 않고 새 dict 를 만든다.
        clearance: 요청자 권한 등급.

    Returns:
        같은 구조의 dict. 도메인마다 등급표를 한 번만 조회해 재사용한다.
    """
    # 도메인별 access_tier 를 메모이제이션 — 같은 도메인 행이 여럿이면 fetch_access_tiers DB 조회를 1회로 묶는다.
    tiers_cache: dict[str, dict[str, str]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    for modality, rows in grouped.items():
        projected: list[dict[str, Any]] = []
        for row in rows:
            domain = str(row.get("domain_label") or "general")
            if domain not in tiers_cache:
                tiers_cache[domain] = fetch_access_tiers(conn, domain)
            summary = row.get("summary") or ""
            masked = project_ext_meta(
                {"summary": summary} if summary else {},
                tiers_cache[domain],
                domain=domain,
                clearance=clearance,
            )
            new_row = dict(row)
            if summary and "summary" not in masked:
                new_row.pop("summary", None)
            elif "summary" in masked:
                new_row["summary"] = masked["summary"]
            projected.append(new_row)
        out[modality] = projected
    return out


def _search_topic_facet(grouped: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """검색 결과에 걸린 자산들이 공유하는 주제를 세어 패싯으로 만든다.

    행에 이미 실려 온 **부모-자식 짝**을 그대로 쓴다 — 자산마다 DB 를 다시 묻지 않는다.
    짝을 쓰는 이유: 부모 목록과 자식 목록을 따로 받아 곱하면, 자산 하나가 여러 주제에
    걸릴 때 있지도 않은 조합이 생긴다.

    Args:
        grouped: 모달리티별 검색 결과 행. 각 행의 주제 짝만 읽고 아무것도 바꾸지 않는다.
            짝이 없는 행은 부모 주제만으로 센다(조합을 만들어 내지 않는다).

    Returns:
        주제별 자산 수와 그 아래 세부주제 분포. 자산 수 내림차순, 동수는 이름순으로
        갈라 순서를 고정한다.
    """
    topic_assets: dict[str, set[str]] = {}
    topic_subs: dict[str, dict[str, set[str]]] = {}  # topic_ko → {subtopic_ko → {asset_id}}
    for rows in grouped.values():
        for r in rows:
            aid = str(r.get("asset_id") or "")
            if not aid:
                continue
            pairs = [str(p) for p in (r.get("topic_pairs") or []) if p]
            if not pairs:
                pairs = [str(t) for t in (r.get("topics") or []) if t]
            for pair in pairs:
                idx = pair.find(">")  # 첫 '>' 로만 자른다 — 세부주제에 '>' 가 섞여도 부모가 어긋나지 않게
                tk = pair if idx < 0 else pair[:idx]
                sk = "" if idx < 0 else pair[idx + 1 :]
                if not tk:
                    continue
                topic_assets.setdefault(tk, set()).add(aid)
                sub_map = topic_subs.setdefault(tk, {})
                if sk:  # subtopic 은 실제 부모 tk 아래에만 귀속(교차곱 제거)
                    sub_map.setdefault(sk, set()).add(aid)
    facet = []
    for tk, assets in topic_assets.items():
        subs = [
            {"subtopic_ko": sk, "asset_count": len(a)}
            for sk, a in topic_subs.get(tk, {}).items()
        ]
        subs.sort(key=lambda s: (-s["asset_count"], s["subtopic_ko"]))
        facet.append({"topic_ko": tk, "asset_count": len(assets), "subtopics": subs})
    facet.sort(key=lambda f: (-f["asset_count"], f["topic_ko"]))
    return facet


def _parse_search_mode(mode: str) -> str:
    """검색 모드 값을 검증한다.

    Args:
        mode: 요청 값. 빈 값이면 ``auto`` 로 본다.

    Returns:
        소문자로 정규화된 모드.

    Raises:
        HTTPException: 허용 밖 값이면 400 — 오타를 기본 모드로 흡수하면 사용자가 의도한
            검색과 다른 결과를 보게 된다.
    """
    m = (mode or "auto").strip().lower()
    if m not in ("auto", "keyword"):
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 mode: {mode!r} (허용: auto, keyword)",
        )
    return m


def _parse_modalities(modalities: str | None) -> list[str] | None:
    """콤마 구분 모달리티 문자열을 검증된 리스트로 파싱한다(미지정=None=전체).

    파싱은 공용 파서 하나만 쓴다(같은 규칙이 두 곳에 생기지 않게).

    Args:
        modalities: 콤마로 이은 모달리티 문자열. ``None``·빈 값이면 **전체**를 뜻한다.

    Returns:
        검증된 목록, 또는 전체를 뜻하는 ``None``.

    Raises:
        HTTPException: 모르는 모달리티가 섞이면 400(조용히 무시하면 사용자는 그 모달리티가
            검색된 줄 안다).
    """
    mods = parse_modalities_csv(modalities)
    if mods is None:
        return None
    unknown = [m for m in mods if m not in VALID_SEARCH_MODALITIES]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 modality: {unknown} (허용: {list(VALID_SEARCH_MODALITIES)})",
        )
    return mods or None


# ── 디버그 뷰(no_cutoff·compact) — 기본 off = 기존 grouped 응답 불변 ──────────────
# 간략 보기는 이미 계산된 결과를 다시 쓰기만 한다 — 검색을 두 번 돌리지 않는다.
# 된 grouped 위에서 계산 — 원시 search_hybrid 버킷 사용 시 tier 미투영 요약 유출이라 정제 후 입력(도메인 배제는 dormant).


def _finite(value: object) -> float:
    """점수를 **유한한 실수**로 정화한다.

    NaN·무한대가 섞이면 정렬 순서가 실행마다 달라진다 — 정렬 전에 여기서 걸러낸다.

    Args:
        value: 어떤 값이든.

    Returns:
        유한 실수. 변환 불가·비유한이면 0.0.
    """
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _clip_text(text: str, max_chars: int) -> str:
    """요약을 한 줄로 펴고 길면 잘라 준다.

    Args:
        text: 원본 텍스트(줄바꿈·연속 공백이 있어도 된다).
        max_chars: 최대 길이. **0 이하면 자르지 않는다**.

    Returns:
        한 줄로 정규화된 텍스트(잘렸으면 끝에 ``…``).
    """
    one_line = " ".join(text.split())
    if max_chars > 0 and len(one_line) > max_chars:
        return one_line[: max_chars - 1].rstrip() + "…"
    return one_line


def _compact_view(
    grouped: dict[str, list[dict[str, Any]]], query: str, limit: int
) -> dict[str, Any]:
    """모달리티 버킷 결과를 한눈에 보기 좋은 단일 랭킹으로 축약한다(디버그·순수).

    각 행은 순위·모달리티·점수·파일명·요약만 남긴다. 점수 내림차순, 동점은 자산 id 순으로
    갈라 순서를 고정한다. 모달리티를 합쳐 상위 ``limit`` 건만 낸다.

    Args:
        grouped: 모달리티별 결과. **이미 권한 투영을 거친 것**을 넣어야 한다 — 원시 결과를
            넣으면 가려야 할 요약이 그대로 노출된다.
        query: 표시용 질의 문자열.
        limit: 낼 행 수 상한.

    Returns:
        축약된 단일 랭킹 dict. 입력 ``grouped`` 는 이미 clearance
    projection 된 portal 결과라 tier 미투영 유출이 없다(도메인 배제는 dormant).
    """
    flat: list[tuple[float, str, dict[str, Any]]] = []
    for modality, rows in grouped.items():
        for r in rows:
            score = round(_finite(r.get("similarity")), 4)
            iid = str(r.get("asset_id", ""))
            flat.append(
                (
                    score,
                    iid,
                    {
                        "모달리티": modality,
                        "점수": score,
                        "파일명": str(r.get("file_name", "")),
                        "요약": _clip_text(str(r.get("summary", "")), _COMPACT_SUMMARY_CHARS),
                    },
                )
            )
    flat.sort(key=lambda t: (-t[0], t[1]))
    top = [{"순위": i, **row} for i, (_s, _id, row) in enumerate(flat[:limit], start=1)]
    return {"query": query, "건수": len(top), "결과": top}


@router.get("/search")
def search(
    q: str = Query(..., description="검색 질의(한국어)"),
    modalities: str | None = Query(
        None, description="콤마 구분: text,image,video,audio (미지정=전체)"
    ),
    size: int = Query(20, ge=1, le=100, description="모달리티별 최대 결과 수(top-N)"),
    limit_per_bucket: int = Query(
        _SEARCH_LIMIT_PER_BUCKET_DEFAULT,
        ge=1,
        le=_SEARCH_LIMIT_PER_BUCKET_MAX,
        description=(
            "버킷당 후보 풀 깊이(top-N=size 캡 이전). 크게 줄수록 컷·도메인배제(dormant) 잔여·074 승격 여지↑, "
            "OS 부하↑. 실제 풀 = max(이 값, size)"
        ),
    ),
    mode: str = Query("auto", description="검색 모드: auto(기본) | keyword(단어 포함 문서)"),
    file_ext: list[str] | None = Query(None, description="파일 확장자 필터(반복 가능, 예: txt,pdf)"),
    created_from: str | None = Query(None, description="생성일 하한(YYYY-MM-DD 또는 ISO datetime, UTC)"),
    created_to: str | None = Query(None, description="생성일 상한(YYYY-MM-DD 또는 ISO datetime, UTC)"),
    topic: str | None = Query(None, description="주제(topic) 정확 일치 필터"),
    subtopic: str | None = Query(None, description="세부주제(subtopic) 정확 일치 필터"),
    no_cutoff: bool = Query(
        False, description="true 면 모달리티별 적합도 컷오프를 무시(약한 매칭까지 노출·디버그용·기본 off)"
    ),
    compact: bool = Query(
        False,
        description="true 면 전 모달리티를 합쳐 점수순 top-K(=size)로 축약(순위·모달리티·점수·파일명·요약·기본 off)",
    ),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """하이브리드 검색 결과를 **모달리티별 그룹**으로 돌려준다.

    모달리티끼리 점수 척도가 달라 **하나의 순위로 합치지 않는다** — 합치면 특정 모달리티가
    통째로 밀려난다. 대신 섹션마다 독립 순위를 매겨 상위 N개씩 내보낸다(전체
    코퍼스 페이징은 아직 없다).
    """
    mods = _parse_modalities(modalities)
    search_mode = _parse_search_mode(mode)
    try:
        search_filters = parse_search_filters(
            file_ext=file_ext,
            created_from=created_from,
            created_to=created_to,
            topic=topic,
            subtopic=subtopic,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"필터 파라미터 형식 오류: {exc}") from exc

    # 풀 하한: 요청 풀이 노출 size 보다 얕으면 size 로 끌어올린다(size 계약 보장 + 승격 여지 확보).
    effective_pool = max(limit_per_bucket, size)
    result = search_hybrid(
        q,
        modalities=mods,
        limit_per_bucket=effective_pool,
        search_mode=search_mode,
        search_filters=search_filters,
        # 디버그용 우회. 기본은 꺼져 있어 평소 호출에는 영향이 없다.
        disable_os_cutoff=no_cutoff,
    )

    # 모달리티별로 독립 순위를 매겨 상위 N개씩 담는다(배제 목록은 현재 비어 있다).
    grouped_raw = group_ranked(result, limit_per_modality=size, exclude_domains=_EXCLUDE_DOMAINS)

    # 권한 투영과 주제 패싯을 **한 트랜잭션에서** 끝낸다 — 연결을 두 번 잡지 않기 위해서다.
    def _project_and_facet(conn: Any) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        """권한별 필드 가리기와 주제 패싯 계산을 **한 번의 조회**로 끝낸다(연결을 두 번 잡지 않게)."""
        projected = _project_grouped_search(conn, grouped_raw, clearance=principal.clearance)
        facet = _search_topic_facet(projected)
        return projected, facet

    grouped, topic_facets = _infra._run_in_db(_project_and_facet)

    # 디버그 opt-in(기본 off): compact 뷰는 이미 clearance projection 된 grouped 위에서 계산(tier 유출 0·도메인 배제 dormant).
    if compact:
        return _compact_view(grouped, q, size)

    counts = {modality: len(rows) for modality, rows in grouped.items()}

    meta: dict[str, Any] = {
        "query": q,
        "modalities": mods,
        "size": size,
        "counts": counts,
        # 이번 결과 안에서만 주제를 센다 — 화면에서 주제를 누르면 그 값으로 다시 필터한다.
        "topic_facets": topic_facets,
    }
    search_plan = (result.get("meta") or {}).get("search_plan")
    if search_plan is not None:
        meta["search_plan"] = search_plan
    # 검색이 남긴 관측치(게이트·검증·정규화)를 응답에 실어 준다 — 있을 때만 넣는다.
    for obs_key in ("os_gate", "llm_verify", "query_norm"):
        obs_val = (result.get("meta") or {}).get(obs_key)
        if obs_val is not None:
            meta[obs_key] = obs_val
    if search_filters is not None:
        meta["filters"] = {
            "file_ext": list(search_filters.file_exts),
            "created_from": search_filters.created_from.isoformat()
            if search_filters.created_from is not None
            else None,
            "created_to": search_filters.created_to.isoformat()
            if search_filters.created_to is not None
            else None,
            "topic": search_filters.topic,
            "subtopic": search_filters.subtopic,
        }

    return {
        "query": q,
        "results": grouped,
        "meta": meta,
    }
