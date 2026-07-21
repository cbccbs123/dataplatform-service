import os
import unittest


def _conn_ctx():
    from dotenv import load_dotenv
    load_dotenv(".env.dev", override=False)
    from src.config.settings import init_settings
    init_settings("dev")
    from src.database.postgres_util import PostgresUtil
    return PostgresUtil()


@unittest.skipUnless(os.environ.get("RUN_DB_E2E") == "1", "실 DB 필요(RUN_DB_E2E=1)")
class TestSnapshotBucketsE2E(unittest.TestCase):
    """054 스냅샷 버킷·timeline 월 집계 실DB 검증(FR-602). 선행: alembic upgrade head(v293)."""

    def test_by_snapshot_bucket_sum_equals_total_and_5buckets(self):
        from service.portal.asset_stats import _SNAPSHOT_BUCKETS, asset_stats
        db = _conn_ctx()
        with db:
            with db.transaction() as conn:
                s = asset_stats(conn, snapshot_buckets=True)
                bkts = s["by_snapshot_bucket"]
                # 5버킷 항상·순서 고정
                self.assertEqual([b["bucket"] for b in bkts], list(_SNAPSHOT_BUCKETS))
                # 합 == total(의료 제외 동일 스코프) — 도넛 정합
                self.assertEqual(sum(b["count"] for b in bkts), s["total"])

    def test_relation_proposed_registered_partition(self):
        # relation_proposed + registered 합 == 전체 registered(상호배타·전수 커버)
        from service.portal.asset_stats import query_assets
        db = _conn_ctx()
        with db:
            with db.transaction() as conn:
                rp = query_assets(conn, snapshot_bucket="relation_proposed", relation_scope="alltime", limit=1)
                reg = query_assets(conn, snapshot_bucket="registered", relation_scope="alltime", limit=1)
                total_registered = conn.execute(
                    "SELECT count(*) FROM asset WHERE domain_label <> 'medical' AND status = 'registered'"
                ).fetchone()[0]
                self.assertEqual(rp["total"] + reg["total"], total_registered)
                # rp total == registered ∧ EXISTS(relations.proposed.v1)
                exp_rp = conn.execute(
                    "SELECT count(*) FROM asset a WHERE a.domain_label <> 'medical' AND a.status = 'registered' "
                    "AND EXISTS (SELECT 1 FROM asset_lineage l WHERE l.asset_id = a.asset_id "
                    "AND l.activity = 'relations.proposed.v1')"
                ).fetchone()[0]
                self.assertEqual(rp["total"], exp_rp)

    def test_snapshot_bucket_pagination_envelope(self):
        # FR-701: 목록 페이징 봉투(맨앞/맨끝 이동) — limit/offset echo·total 정확
        from service.portal.asset_stats import query_assets
        db = _conn_ctx()
        with db:
            with db.transaction() as conn:
                out = query_assets(conn, snapshot_bucket="processing", limit=5, offset=0)
                self.assertEqual(out["limit"], 5)
                self.assertEqual(out["offset"], 0)
                self.assertLessEqual(len(out["rows"]), 5)
                self.assertIsInstance(out["total"], int)

    def test_timeline_month_group_by_modality(self):
        from service.portal.asset_stats import asset_timeline
        db = _conn_ctx()
        with db:
            with db.transaction() as conn:
                out = asset_timeline(conn, interval="month", group_by="modality")
                self.assertEqual(out["interval"], "month")
                self.assertIn("series", out)
