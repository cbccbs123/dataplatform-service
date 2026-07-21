"""파일 확장자 SQL 조각 단일 출처(069 D4·057 FR-104). ``asset_stats``·``lineage_query`` 공용.

fs_path 마지막 ``.세그먼트`` 를 소문자로 뽑는 확장자 정규식이다. 통합 전에는 같은 정규식이
3벌(asset_stats 집계·투영식 / asset_stats 필터 / lineage_query)로 흩어져 있었다. **조회 행의
``file_ext`` 값과 확장자 집계 버킷 키가 매칭되려면 둘이 같은 SQL 표현식이어야 하므로**(057 FR-104),
표현식을 여기 한 곳에서만 만든다(리터럴 단일 정의처·표류 방지).

고정 SQL·raw 정규식(사용자 입력 아님·인젝션 안전). ``prefix`` 는 fs_path 컬럼 접두다.
"""

from __future__ import annotations


def ext_expr(prefix: str = "") -> str:
    r"""``lower(substring(<prefix>fs_path from '\.([^./]+)$'))`` SQL 조각을 반환한다(순수·결정적).

    ``prefix`` = fs_path 컬럼 접두:
        - ``""``   단일 테이블(asset) 쿼리 — 비한정 ``fs_path``.
        - ``"a."`` JOIN 쿼리 — 별칭으로 모호성 차단.
        - ``"{a}"`` asset_stats 필터 템플릿 — 나중에 ``.format(a=pfx)`` 로 접두를 바꾼다
          (정규식 본문에 ``{`` / ``}`` 가 없어 format 안전).
    """
    return rf"lower(substring({prefix}fs_path from '\.([^./]+)$'))"
