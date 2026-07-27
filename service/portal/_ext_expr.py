"""파일 확장자를 뽑는 SQL 조각의 **단일 출처** — 자산 집계와 계보 조회가 함께 쓴다.

경로의 마지막 ``.세그먼트`` 를 소문자로 뽑는다.

**여기 하나만 둔다**: 목록에 찍히는 확장자와 집계의 버킷 키가 맞물리려면 둘이 같은 식으로
뽑혀야 한다. 식이 두 벌이 되는 순간 "목록엔 있는데 통계엔 없는" 확장자가 생긴다.

만들어 내는 SQL 은 고정 문자열이다 — 사용자 입력이 끼어들 자리가 없다.
"""

from __future__ import annotations


def ext_expr(prefix: str = "") -> str:
    r"""``lower(substring(<prefix>fs_path from '\.([^./]+)$'))`` SQL 조각을 반환한다(순수·결정적).

    ``prefix`` = fs_path 컬럼 접두:
        - ``""``   단일 테이블(asset) 쿼리 — 비한정 ``fs_path``.
        - ``"a."`` JOIN 쿼리 — 별칭으로 모호성 차단.
        - ``"{a}"`` asset_stats 필터 템플릿 — 나중에 ``.format(a=pfx)`` 로 접두를 바꾼다
          (정규식 본문에 ``{`` / ``}`` 가 없어 format 안전).

    Args:
        prefix: 컬럼 접두사. **틀리면 조인 경로에서 컬럼이 모호해져 쿼리가 실패한다**.

    Returns:
        확장자 추출 SQL 조각. 집계와 목록이 **같은 값**을 써야 수치가 맞물린다.
    """
    return rf"lower(substring({prefix}fs_path from '\.([^./]+)$'))"
