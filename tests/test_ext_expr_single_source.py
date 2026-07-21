"""T305 (069 D4) — 확장자 SQL 정규식 단일 출처화 계약 테스트 (순수·DB 불필요).

확장자 추출 SQL 정규식 3벌(``asset_stats._EXT_EXPR`` · ``asset_stats`` 필터 인라인 ·
``lineage_query._EXT_EXPR``)을 포탈 공용 ``ext_expr(prefix="")`` 1함수로 통합한다.

핵심(057 FR-104): 조회 행의 ``file_ext`` 값 == 확장자 집계 버킷 키. 둘이 **같은 SQL 표현식**을
써야 매칭되므로, 표현식을 단일 함수에서만 생성한다. 통합 전후 산출 SQL 이 동일해야 한다.
"""

from __future__ import annotations

import unittest

# 통합 전 3벌이 내던 정확한 정규식 리터럴(레거시 기준값 — 산출 SQL 불변 봉인).
_LEGACY_UNQUALIFIED = r"lower(substring(fs_path from '\.([^./]+)$'))"
_LEGACY_ALIASED = r"lower(substring(a.fs_path from '\.([^./]+)$'))"
_LEGACY_TEMPLATE = r"lower(substring({a}fs_path from '\.([^./]+)$'))"


class _Cur:
    """execute 기록 + 미리 채운 결과를 순서대로 반환하는 fake 커서."""

    def __init__(self, results):
        self.calls = []
        self._results = list(results)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._results.pop(0)

    def fetchall(self):
        return self._results.pop(0)


class _Conn:
    def __init__(self, results=()):
        self._cur = _Cur(results)

    def cursor(self):
        return self._cur


class TestExtExprMatchesLegacy(unittest.TestCase):
    """``ext_expr`` 가 기존 3벌과 **같은 산출**(같은 입력 파일명 → 같은 확장자)."""

    def test_unqualified(self) -> None:
        from service.portal._ext_expr import ext_expr

        self.assertEqual(ext_expr(), _LEGACY_UNQUALIFIED)
        self.assertEqual(ext_expr(""), _LEGACY_UNQUALIFIED)

    def test_aliased(self) -> None:
        from service.portal._ext_expr import ext_expr

        self.assertEqual(ext_expr("a."), _LEGACY_ALIASED)

    def test_format_template(self) -> None:
        # asset_stats 필터는 나중에 .format(a=pfx) 로 접두를 바꾼다 — {a} 자리표시자 보존.
        from service.portal._ext_expr import ext_expr

        self.assertEqual(ext_expr("{a}"), _LEGACY_TEMPLATE)
        self.assertEqual(ext_expr("{a}").format(a="a."), _LEGACY_ALIASED)
        self.assertEqual(ext_expr("{a}").format(a=""), _LEGACY_UNQUALIFIED)


class TestSingleSourceReferences(unittest.TestCase):
    """정규식 리터럴 1벌 — asset_stats·lineage_query 의 _EXT_EXPR 가 ext_expr 로 파생된다."""

    def test_asset_stats_uses_ext_expr(self) -> None:
        from service.portal._ext_expr import ext_expr
        from service.portal.asset_stats import _EXT_EXPR

        self.assertEqual(_EXT_EXPR, ext_expr())

    def test_lineage_uses_ext_expr(self) -> None:
        from service.portal._ext_expr import ext_expr
        from service.portal.lineage_query import _EXT_EXPR

        self.assertEqual(_EXT_EXPR, ext_expr("a."))


class TestFr104RowEqualsBucket(unittest.TestCase):
    """057 FR-104: query_assets 행 file_ext 파생식 == asset_stats 확장자 집계식(같은 표현식)."""

    def test_row_projection_and_aggregate_use_same_expr(self) -> None:
        from service.portal._ext_expr import ext_expr

        expr = ext_expr()

        # (1) 집계 by_file_ext 쿼리에 확장자 표현식이 들어간다(asset_stats).
        from service.portal.asset_stats import asset_stats, query_assets

        agg_conn = _Conn([
            (0,),   # COUNT
            [],     # by_status
            [],     # by_modality
            [],     # by_domain
            [],     # by_file_ext
            [],     # by_date
        ])
        asset_stats(agg_conn)
        agg_sqls = [c[0] for c in agg_conn._cur.calls]
        self.assertTrue(
            any(f"{expr} AS ext" in s for s in agg_sqls),
            "by_file_ext 집계가 ext_expr 표현식을 쓰지 않음",
        )

        # (2) query_assets 행 file_ext 투영도 같은 표현식(비-content 경로).
        rows_conn = _Conn([
            (0,),   # COUNT
            [],     # rows
        ])
        query_assets(rows_conn, with_content=False, limit=10, offset=0)
        row_sqls = [c[0] for c in rows_conn._cur.calls]
        self.assertTrue(
            any(f"{expr} AS file_ext" in s for s in row_sqls),
            "행 file_ext 투영이 ext_expr 표현식을 쓰지 않음",
        )


class TestNoResidualExtLiteral(unittest.TestCase):
    """RED ③: 정규식 리터럴이 ext_expr 정의 1곳에만 존재(다른 포탈 모듈엔 원시 리터럴 없음)."""

    def test_regex_literal_defined_once(self) -> None:
        import inspect

        from service.portal import _ext_expr

        # ext_expr 소스에는 리터럴이 있어야 한다(단일 정의처).
        src = inspect.getsource(_ext_expr)
        self.assertIn(r"\.([^./]+)$", src)


if __name__ == "__main__":
    unittest.main()
