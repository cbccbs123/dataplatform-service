"""052 HITL 관계 검토 API — 실 DB 라운드트립 e2e(SC-005·RUN_DB_E2E 게이트).

무DB 환경에서는 자동 skip(다른 ``*_e2e`` 관례 일치). ``RUN_DB_E2E=1`` + 로컬
PostgreSQL 에서만 실행한다.

시나리오(SC-005)
    proposed 엣지 1건 시드 → ``list_edges_for_review(status="proposed")`` 노출 →
    ``bulk_review(approve)`` → ``list(status="active")`` 노출 →
    ``revise_edge(to_status="rejected")`` → ``list(status="rejected")`` 노출.
    각 결정 후 ``access_log`` 에 대응 action(relation.approve/relation.revise) 행 존재.

    review.py 함수는 conn 을 받으므로, 포탈 핸들러가 하는 "결정 + 같은 트랜잭션 감사"
    조합을 여기서 직접 재현해 라운드트립 + 감사 무결성을 실 DB 로 확인한다.
"""
from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

from dotenv import load_dotenv

_RUN = os.getenv("RUN_DB_E2E") == "1"
_ENV = Path(__file__).resolve().parents[1] / ".env.dev"


def _vec():
    from src.config.embedding_constants import FIX_EMBEDDING_DIMENSION
    v = [0.0] * FIX_EMBEDDING_DIMENSION
    v[0] = 0.5
    return v


def _make_registered_asset(db, ids: list) -> str:
    """registered + st 임베딩 보유 자산 1건 생성(graph_persist e2e 헬퍼와 동일 패턴)."""
    from src.dispatch.types import AssetRecord, EmbeddingItem
    from src.ingest.asset_persist import create_asset, finalize_asset
    from src.ingest.status import AssetStatus, set_status

    with db.transaction() as conn:
        aid = create_asset(conn, fs_path=f"/t/{uuid.uuid4().hex}.txt", modality="txt",
                           file_hash=uuid.uuid4().hex)
    ids.append(aid)
    with db.transaction() as conn:
        set_status(conn, aid, AssetStatus.ROUTING)
        set_status(conn, aid, AssetStatus.CLASSIFYING)
        set_status(conn, aid, AssetStatus.EXTRACTING)
    with db.transaction() as conn:
        finalize_asset(conn, aid, AssetRecord(
            embeddings=[EmbeddingItem(channel="st", vector=_vec(), model_name="m")]))
    return str(aid)


@unittest.skipUnless(_RUN, "RUN_DB_E2E=1 일 때만")
class TestRelationsReviewApiDB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dotenv(_ENV, override=False)
        from src.database.postgres_util import PostgresUtil
        cls.db = PostgresUtil()
        cls.db.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.db.__exit__(None, None, None)

    def setUp(self):
        self._ids: list = []
        self._reviewer = f"rev-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        # 감사 로그·엣지·자산 정리(테스트 격리). access_log 는 reviewer 로 스코프.
        with self.db.transaction() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM access_log WHERE user_id = %s", (self._reviewer,))
            if self._ids:
                cur.execute("DELETE FROM asset WHERE asset_id = ANY(%s)", (self._ids,))

    def _seed_proposed_edge(self) -> str:
        """proposed same_domain 엣지 1건 시드 → edge_id 반환.

        auto_approve_min 기본 1.01(신뢰도가 1.0 초과 불가 → 자동승인 없음)이라 status=proposed.
        confidence=1.0 은 실 dev DB(status별 수천 건)에서도 `confidence DESC` 정렬 최상단에
        오게 해, 목록 조회(페이징) 첫 페이지에 시드 엣지가 확실히 잡히게 한다(1.0<1.01 이라 여전히
        proposed). 단, active 에는 이미 conf=1.0 엣지가 다수 있을 수 있어 순회 조회로 보강한다.
        """
        from src.relations.graph_persist import sync_graph_edges
        src_id = _make_registered_asset(self.db, self._ids)
        dst_id = _make_registered_asset(self.db, self._ids)
        edges = [{
            "target_media_item_id": dst_id, "relation_type_code": "same_domain",
            "topic_ko": "일반", "topic_en": "general", "subtopic_ko": "", "subtopic_en": "",
            "confidence": 1.0, "reason": "e2e 검토",
        }]
        up, _ = self.db.execute_in_transaction(
            lambda conn: sync_graph_edges(
                conn, source_asset_id=src_id, edges=edges,
                allowed_target_ids=frozenset({dst_id})),
            idempotent=False)
        self.assertEqual(up, 1)
        # 방금 만든 엣지의 edge_id 조회(양끝 자산으로 특정).
        with self.db.transaction() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT ge.edge_id FROM graph_edge ge
                JOIN node sn ON sn.node_id = ge.src_node
                JOIN node dn ON dn.node_id = ge.dst_node
                WHERE (sn.asset_id = %s OR dn.asset_id = %s)
                  AND (sn.asset_id = %s OR dn.asset_id = %s)
            """, (src_id, src_id, dst_id, dst_id))
            row = cur.fetchone()
        self.assertIsNotNone(row)
        return str(row[0])

    def _find_edge_in_list(self, edge_id: str, status: str) -> dict | None:
        # 시드 엣지는 confidence=1.0 이라 `confidence DESC` 정렬 최상단(첫 페이지)에 온다
        # (실 dev DB 수천 건 중에도) — 단일 페이지 조회로 충분하다. edge_id 는 review.py 가
        # str 로 반환하므로 str 비교(_seed 도 str) 가 성립한다.
        from src.relations.review import list_edges_for_review
        result = self.db.execute_in_transaction(
            lambda conn: list_edges_for_review(conn, status=status, limit=200, offset=0),
            idempotent=True)
        for r in result["rows"]:
            if r["edge_id"] == edge_id:
                return r
        return None

    def _count_audit(self, action: str) -> int:
        with self.db.transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM access_log WHERE user_id = %s AND action = %s",
                (self._reviewer, action))
            (n,) = cur.fetchone()
        return int(n)

    def _find_edge_filtered(self, edge_id: str, status: str, **filters) -> dict | None:
        """G7 확장 — 검색·필터·기간 인자를 그대로 list_edges_for_review 에 넘겨 시드 엣지 탐색.

        시드 confidence=1.0 이라 confidence DESC 정렬 최상단(첫 페이지) 보장(실 DB 수천 건에도).
        """
        from src.relations.review import list_edges_for_review
        result = self.db.execute_in_transaction(
            lambda conn: list_edges_for_review(
                conn, status=status, limit=200, offset=0, **filters),
            idempotent=True)
        for r in result["rows"]:
            if r["edge_id"] == edge_id:
                return r
        return None

    def test_roundtrip_proposed_approve_revise_rejected(self):
        from service.portal.access_log import record_access
        from src.relations.review import bulk_review, revise_edge

        edge_id = self._seed_proposed_edge()

        # 1) proposed 목록에 노출(식별 보강 필드 존재)
        row = self._find_edge_in_list(edge_id, "proposed")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind_code"], "same_domain")
        self.assertIn("file_name", row["src"])
        self.assertIn("file_name", row["dst"])

        # 2) approve → active + 감사(결정 + 같은 트랜잭션 감사, 포탈 핸들러 재현)
        def _approve(conn):
            results = bulk_review(conn, edge_ids=[edge_id], reviewer=self._reviewer,
                                  action="approve")
            for r in results:
                if r["ok"]:
                    record_access(conn, action="relation.approve", user_id=self._reviewer,
                                  detail={"edge_id": r["edge_id"]})
            return results

        results = self.db.execute_in_transaction(_approve, idempotent=False)
        self.assertEqual(results, [{"edge_id": edge_id, "ok": True}])
        self.assertIsNotNone(self._find_edge_in_list(edge_id, "active"))
        self.assertIsNone(self._find_edge_in_list(edge_id, "proposed"))
        self.assertEqual(self._count_audit("relation.approve"), 1)

        # 3) revise → rejected + 감사(사람 전용·proposed 가드 없음)
        def _revise(conn):
            ok = revise_edge(conn, edge_id=edge_id, reviewer=self._reviewer,
                             to_status="rejected")
            if ok:
                record_access(conn, action="relation.revise", user_id=self._reviewer,
                              detail={"edge_id": edge_id, "to_status": "rejected"})
            return ok

        self.assertTrue(self.db.execute_in_transaction(_revise, idempotent=False))
        self.assertIsNotNone(self._find_edge_in_list(edge_id, "rejected"))
        self.assertIsNone(self._find_edge_in_list(edge_id, "active"))
        self.assertEqual(self._count_audit("relation.revise"), 1)

    def test_filter_by_q_kind_and_period(self):
        """SC-016 — 시드 엣지가 q(파일명 조각)·kind_code·기간(created_at) 필터로 잡히고,
        엉뚱한 필터(미지 kind_code·미래 기간)에는 안 잡힌다(0건).
        """
        import datetime as _dt

        edge_id = self._seed_proposed_edge()

        # proposed 엣지의 src/dst 파일명(basename) 조각을 얻어 q 검색에 쓴다.
        base_row = self._find_edge_in_list(edge_id, "proposed")
        self.assertIsNotNone(base_row)
        file_name = base_row["src"]["file_name"]  # 예: <hex>.txt
        q_frag = file_name.split(".")[0][:8]  # 파일명 일부(hex 앞자락)

        # 1) q(파일명 조각) + kind_code(same_domain) → 잡힌다.
        self.assertIsNotNone(
            self._find_edge_filtered(edge_id, "proposed", q=q_frag, kind_code="same_domain"))

        # 2) 미지 kind_code → 0건(검증 없이 total=0·FR-703).
        self.assertIsNone(
            self._find_edge_filtered(edge_id, "proposed", kind_code="__no_such_kind__"))

        # 3) created_at 기간(넉넉한 과거~미래) → 잡힌다. date_col=created_at(proposed 기본).
        wide_since = _dt.datetime(2000, 1, 1)
        wide_until = _dt.datetime(2100, 1, 1)
        self.assertIsNotNone(
            self._find_edge_filtered(edge_id, "proposed",
                                     since=wide_since, until=wide_until, date_col="created_at"))

        # 4) 과거 종료(먼 과거로 until 컷) → 0건(엣지 created_at 이 그 뒤).
        self.assertIsNone(
            self._find_edge_filtered(edge_id, "proposed",
                                     until=_dt.datetime(2001, 1, 1), date_col="created_at"))

        # 5) confidence 범위(0.9~1.0) → 시드 conf=1.0 이라 잡힌다.
        self.assertIsNotNone(
            self._find_edge_filtered(edge_id, "proposed",
                                     min_confidence=0.9, max_confidence=1.0))
