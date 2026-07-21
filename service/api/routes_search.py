"""검색 라우트 (069 US-E FR-E6·A) — 하이브리드 검색 + tier projection·주제 패싯·디버그 뷰(compact/group).

종전 ``portal_api.py`` 의 ``/search`` 핸들러와 그 순수 helper를 그대로 이관한다(동작 불변). ``search_hybrid``·
``group_ranked``·``fetch_active_relations_for_asset``·``fetch_access_tiers`` 는 홈에서 직접 import 하고,
인프라(``_run_in_db``)는 ``_infra`` 모듈참조로 쓴다 — 테스트 patch 정본은 ``service.api.routes_search.<name>``.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from service.api import _infra
from src.config.search_modalities import VALID_SEARCH_MODALITIES, parse_modalities_csv
from service.portal.auth import Principal, require_principal
from service.portal.search_group import group_ranked
from src.registry.access_tier import project_ext_meta
from src.registry.ext_meta_field_registry import fetch_access_tiers
from src.relations.graph_query import fetch_active_relations_for_asset
from src.search.search_filters import parse_search_filters
from src.search.search_service import search_hybrid

router = APIRouter()

# 의료(PHI) 배제 도메인 집합(FR-014). group_ranked 가 각 모달리티 버킷에서 이 도메인 행을 제거한다.
_EXCLUDE_DOMAINS = frozenset({"medical"})

# search_hybrid 의 버킷당 후보 풀 **기본값**. /search 의 limit_per_bucket 로 요청마다 덮어쓴다.
# 응답은 모달리티별 top-N(size)으로 자르지만 풀은 그보다 깊게 받아야 (a)의료 배제 잔여 (b)074 검증
# 드롭 후 승격 여지가 생긴다 — 핸들러가 max(풀, size)로 하한을 걸어 풀<size 회귀를 막는다.
_SEARCH_LIMIT_PER_BUCKET_DEFAULT = 50
# 풀 상한(요청 남용·OS 부하 방어). size 상한(100)보다 넉넉히 둬 승격 여지를 남긴다.
_SEARCH_LIMIT_PER_BUCKET_MAX = 500

# 같은 소스(영상)로 묶을 때 쓰는 관계 종류. 같은 영상의 모달리티 간 근접중복/파생만 — 주제(same_domain)는
# 다른 영상을 잇는 관계라 묶음에서 제외(메가블롭 방지, 2026-06-05 진단으로 확정·sample 보존).
_SAME_SOURCE_KINDS = ("duplicate_near", "derived_from")


def _project_grouped_search(
    conn: Any,
    grouped: dict[str, list[dict[str, Any]]],
    *,
    clearance: str,
) -> dict[str, list[dict[str, Any]]]:
    """검색 hit ``summary`` 에 tier 기반 키 omit (042).

    ``summary`` 를 mini ext_meta 로 ``project_ext_meta`` 에 넘김 — 미달 시 행에서 ``summary`` 키 제거.
    OpenSearch 색인은 변경 없음(API 응답 단계만).
    """
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
    """검색 결과(의료 배제·top-N)의 자산들이 공유하는 주제 패싯을 집계한다(056 FR-503·US3).

    각 결과 행에 이미 실린 **색인 topic_pairs**(059)로 ``topic_ko`` 별 distinct 결과-자산 수와, 그 아래
    실제 부모의 ``subtopic_ko`` 별 결과-자산 수(nested)를 센다. 라이브 투영·자산당 DB 호출(N+1) 없이
    행 값만 집계. **060 짝 정밀화**: nested 를 부모-자식 짝(topic_pairs·첫 ``>`` 분할) 기준으로 귀속해
    멀티토픽 오배치를 제거한다. topic_pairs 부재 시 평면 topics 를 topic 단독 짝으로 폴백(교차곱 없음).
    신규 LLM 0·조회 0·결정적 정렬(asset_count desc → topic_ko/subtopic_ko asc·헌법 3조).
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
                idx = pair.find(">")  # 첫 '>' 로만 분할(059 파싱 계약)
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
    """검색 mode 파라미터 검증(044 — auto|keyword)."""
    m = (mode or "auto").strip().lower()
    if m not in ("auto", "keyword"):
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 mode: {mode!r} (허용: auto, keyword)",
        )
    return m


def _parse_modalities(modalities: str | None) -> list[str] | None:
    """콤마 구분 모달리티 문자열을 검증된 리스트로 파싱한다(미지정=None=전체).

    파싱은 공유 파서 ``parse_modalities_csv`` 단일 출처(069 T301), 알 수 없는 모달리티는 현행대로
    ``HTTPException(400)`` 으로 거부한다(포탈 검증 계약 보존).
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


# ── 069 T407: 삭제된 sample_search_api 디버그 3종을 /search opt-in 으로 이관 ──────────────
# 세 뷰(no_cutoff·compact·group_by_relation)는 전부 **기본 off = 기존 grouped 응답 불변**이다.
# compact/group 뷰는 이미 group_ranked 로 **의료 배제·clearance projection 된 grouped** 위에서 계산한다
# — 원시 search_hybrid 버킷을 쓰면 의료 유출이므로(헌법 10조) 반드시 정제 후 결과를 입력으로 쓴다.


def _finite(value: object) -> float:
    """None/NaN/inf/비수치 → 0.0 인 유한 실수(점수 정화·순수)."""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _clip_text(text: str, max_chars: int) -> str:
    """요약을 한 줄로 정규화하고 max_chars 초과분은 …로 자른다(한눈에 보기용·순수)."""
    one_line = " ".join(text.split())
    if max_chars > 0 and len(one_line) > max_chars:
        return one_line[: max_chars - 1].rstrip() + "…"
    return one_line


def _stem_name(fn: str) -> str:
    """파일명에서 확장자/파생 접미사를 떼 '소스 영상' 이름만(.ko.txt/.stt.txt 포함 처리·순수)."""
    for suf in (".ko.txt", ".stt.txt"):
        if fn.endswith(suf):
            return fn[: -len(suf)]
    return fn.rsplit(".", 1)[0] if "." in fn else fn


def _compact_view(
    grouped: dict[str, list[dict[str, Any]]], query: str, limit: int, summary_max: int
) -> dict[str, Any]:
    """모달리티 버킷 결과를 한눈에 보기 좋은 단일 랭킹으로 축약한다(디버그·순수).

    각 행은 순위·모달리티·합산 점수(similarity)·파일명·요약만 남긴다. 점수 내림차순, 동점은 asset_id
    오름차순(결정적·헌법 3조). 전 모달리티 합쳐 상위 ``limit`` 건만. 입력 ``grouped`` 는 이미 의료 배제·
    projection 된 portal 결과라 의료 유출이 없다.
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
                        "요약": _clip_text(str(r.get("summary", "")), summary_max),
                    },
                )
            )
    flat.sort(key=lambda t: (-t[0], t[1]))
    top = [{"순위": i, **row} for i, (_s, _id, row) in enumerate(flat[:limit], start=1)]
    return {"query": query, "건수": len(top), "결과": top}


def _fetch_same_source_edges(asset_ids: list[str]) -> list[tuple[str, str]]:
    """히트 자산들 사이의 '같은 소스' 묶음 쌍을 (a,b) 로 조회한다(graph_query seam·의료 배제).

    069 T407: sample 의 순진한 ``WHERE src_node=X`` 단방향 SQL(CR-19·대칭 엣지 dst 접힘 누락) +
    요청마다 PostgresUtil() 생성(P1-1)을 폐기하고, ``_run_in_db`` 싱글턴 풀 + graph_query 공개
    read(``fetch_active_relations_for_asset``·대칭 엣지 양방향 정규화)로 교체한다. 히트 집합 내부 이웃 중
    kind 가 same-source(duplicate_near/derived_from)인 쌍만 모은다. status='active' 만·양끝 의료 배제는
    seam 이 보장한다.

    트레이드오프(seam 재사용의 대가): sample 원본은 단일 SQL 1회였으나 여기선 asset 수만큼 seam 순차
    호출(N 왕복). group_by_relation 은 opt-in 디버그 뷰·히트 상한이 작아(size×모달리티) 문제되지 않는다.
    대량화되면 graph_query 에 다중 asset_id 배치 read 오버로드 추가 검토(069 코드리뷰 2026-07-15 🟡).
    """
    ids = [a for a in asset_ids if a]
    if not ids:
        return []
    id_set = set(ids)

    def _run(conn: Any) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for aid in ids:
            for nb in fetch_active_relations_for_asset(conn, asset_id=aid, status="active"):
                other = nb.get("asset_id")
                if nb.get("kind_code") in _SAME_SOURCE_KINDS and other in id_set:
                    pairs.append((aid, str(other)))
        return pairs

    return _infra._run_in_db(_run)


def _group_by_relation_view(
    grouped: dict[str, list[dict[str, Any]]], query: str, summary_max: int
) -> dict[str, Any]:
    """검색 결과를 '같은 소스 영상' 단위로 묶어 보여준다(graph_edge 관계 기반·디버그).

    같은 영상의 여러 모달리티(이미지·영상·오디오·텍스트)가 한 묶음으로 접힌다. manifest.json 류 허브는
    제외. 묶음점수=구성원 max, 정렬은 점수 내림차순(동점은 대표 asset_id·결정적·헌법 3조). 입력
    ``grouped`` 는 이미 의료 배제·projection 된 portal 결과다.
    """
    info: dict[str, dict[str, Any]] = {}
    for modality, rows in grouped.items():
        for r in rows:
            aid = str(r.get("asset_id") or "")
            fn = str(r.get("file_name", ""))
            if not aid or fn == "manifest.json":
                continue
            score = round(_finite(r.get("similarity")), 4)
            prev = info.get(aid)
            if prev is None or score > prev["점수"]:
                info[aid] = {
                    "asset_id": aid,
                    "모달리티": modality,
                    "점수": score,
                    "파일명": fn,
                    "요약": _clip_text(str(r.get("summary", "")), summary_max),
                }

    parent = {a: a for a in info}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in _fetch_same_source_edges(list(info)):
        if a in parent and b in parent:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[ra] = rb

    groups: dict[str, list[dict[str, Any]]] = {}
    for aid in info:
        groups.setdefault(_find(aid), []).append(info[aid])

    bundles = []
    for members in groups.values():
        members.sort(key=lambda m: (-m["점수"], m["asset_id"]))
        rep = members[0]
        bundles.append(
            {
                "_rep_id": rep["asset_id"],
                "묶음점수": rep["점수"],
                "대표": _stem_name(rep["파일명"]),
                "요약": rep["요약"],
                "구성": [
                    {"모달리티": m["모달리티"], "점수": m["점수"], "파일명": m["파일명"]}
                    for m in members
                ],
            }
        )
    bundles.sort(key=lambda bz: (-bz["묶음점수"], bz["_rep_id"]))
    out = [
        {"순위": i, **{k: v for k, v in b.items() if k != "_rep_id"}}
        for i, b in enumerate(bundles, start=1)
    ]
    return {"query": query, "묶음수": len(out), "묶음": out}


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
            "버킷당 후보 풀 깊이(top-N=size 캡 이전). 크게 줄수록 의료배제 잔여·074 승격 여지↑, "
            "OS 부하↑. 실제 풀 = max(이 값, size)"
        ),
    ),
    mode: str = Query("auto", description="검색 모드: auto(기본) | keyword(단어 포함 문서)"),
    file_ext: list[str] | None = Query(None, description="파일 확장자 필터(반복 가능, 예: txt,pdf)"),
    source_dataset: list[str] | None = Query(
        None, description="출처 데이터셋 필터(반복 가능: data1~3, wikipedia, youtube, unknown)"
    ),
    created_from: str | None = Query(None, description="생성일 하한(YYYY-MM-DD 또는 ISO datetime, UTC)"),
    created_to: str | None = Query(None, description="생성일 상한(YYYY-MM-DD 또는 ISO datetime, UTC)"),
    topic: str | None = Query(None, description="주제(topic) 정확 일치 필터(056·keyword terms)"),
    subtopic: str | None = Query(None, description="세부주제(subtopic) 정확 일치 필터(056·keyword terms)"),
    must_include: list[str] | None = Query(
        None, description="반드시 포함 텀(반복 가능·BM25 must·전체 코퍼스 기준·057 FR-202)"
    ),
    must_exclude: list[str] | None = Query(
        None, description="반드시 제외 텀(반복 가능·BM25 must_not·전체 코퍼스 기준·057 FR-202)"
    ),
    no_cutoff: bool = Query(
        False, description="true 면 모달리티별 적합도 컷오프를 무시(약한 매칭까지 노출·027 디버그·기본 off)"
    ),
    compact: bool = Query(
        False,
        description="true 면 전 모달리티를 합쳐 점수순 top-K(=size)로 축약(순위·모달리티·점수·파일명·요약·기본 off)",
    ),
    group_by_relation: bool = Query(
        False,
        description="true 면 관계(graph_edge active duplicate_near/derived_from)로 같은 소스 영상의 "
        "모달리티를 한 묶음으로 접어 반환(compact 보다 우선·기본 off)",
    ),
    summary_chars: int = Query(
        160, ge=0, le=2000, description="compact/묶음 뷰 요약 최대 글자수(0=자르지 않음·off 뷰엔 무영향)"
    ),
    principal: Annotated[Principal, Depends(require_principal)] = ...,
) -> dict[str, Any]:
    """006 하이브리드 검색을 **모달리티별 그룹**으로 반환한다(FR-001/002/003 + 056 FR-503).

    내부: ``search_hybrid``(신규 LLM 호출 0, 006 seam) → ``group_ranked``(모달리티별 독립 랭킹·의료 배제,
    FR-014). 모달리티 간 점수 척도가 비교 불가라 단일 랭킹으로 합치지 않고 섹션별로 제공한다. 섹션별
    top-N(``size``), 페이징 없음(전체 코퍼스 keyset 페이징은 006 재설계 후속).
    """
    mods = _parse_modalities(modalities)
    search_mode = _parse_search_mode(mode)
    try:
        search_filters = parse_search_filters(
            file_ext=file_ext,
            source_dataset=source_dataset,
            created_from=created_from,
            created_to=created_to,
            topic=topic,
            subtopic=subtopic,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"필터 파라미터 형식 오류: {exc}") from exc

    # 057 FR-202: 반복 쿼리 파라미터(must_include/must_exclude)를 strip·빈문자열 제거·순서 보존으로
    # 정규화(미지정 None → []). 빈 리스트면 OS 본문이 바이트 동일(하위호환·회귀 0).
    inc_terms = [s for s in ((t or "").strip() for t in (must_include or [])) if s]
    exc_terms = [s for s in ((t or "").strip() for t in (must_exclude or [])) if s]

    # 풀 하한: 요청 풀이 노출 size 보다 얕으면 size 로 끌어올린다(size 계약 보장 + 승격 여지 확보).
    effective_pool = max(limit_per_bucket, size)
    result = search_hybrid(
        q,
        modalities=mods,
        limit_per_bucket=effective_pool,
        search_mode=search_mode,
        search_filters=search_filters,
        must_include=inc_terms,
        must_exclude=exc_terms,
        # 069 T407: 디버그 opt-in — 기본 False 라 미지정 시 기존 호출과 동작 불변(027 컷 유지).
        disable_os_cutoff=no_cutoff,
    )

    # FR-014: 버킷별 의료 배제 + 모달리티별 독립 랭킹·top-N. results 는 {modality: [rows]}.
    grouped_raw = group_ranked(result, limit_per_modality=size, exclude_domains=_EXCLUDE_DOMAINS)

    # tier projection(042)과 주제 패싯(056 FR-503)을 **같은 읽기 트랜잭션**에서 계산한다(풀 1회).
    def _project_and_facet(conn: Any) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
        projected = _project_grouped_search(conn, grouped_raw, clearance=principal.clearance)
        facet = _search_topic_facet(projected)
        return projected, facet

    grouped, topic_facets = _infra._run_in_db(_project_and_facet)

    # 069 T407(디버그 opt-in·기본 off): group_by_relation > compact 우선순위(sample 현행 보존).
    # 두 뷰 모두 이미 의료 배제·projection 된 grouped 위에서 계산 → 의료 자산 유출 0(헌법 10조).
    if group_by_relation:
        return _group_by_relation_view(grouped, q, summary_chars)
    if compact:
        return _compact_view(grouped, q, size, summary_chars)

    counts = {modality: len(rows) for modality, rows in grouped.items()}

    meta: dict[str, Any] = {
        "query": q,
        "modalities": mods,
        "size": size,
        "counts": counts,
        # 056 FR-503(US3): 결과-스코프 주제 패싯 집계(topic_ko별 결과-자산 수). 주제 클릭 → topic= 필터.
        "topic_facets": topic_facets,
    }
    search_plan = (result.get("meta") or {}).get("search_plan")
    if search_plan is not None:
        meta["search_plan"] = search_plan
    # 069 P1-4: search_hybrid 관측성 meta 전파 — os_gate(027)+llm_verify(074)+query_norm(075). 있을 때만.
    for obs_key in ("os_gate", "llm_verify", "query_norm"):
        obs_val = (result.get("meta") or {}).get(obs_key)
        if obs_val is not None:
            meta[obs_key] = obs_val
    if search_filters is not None:
        meta["filters"] = {
            "file_ext": list(search_filters.file_exts),
            "source_dataset": list(search_filters.source_datasets),
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
