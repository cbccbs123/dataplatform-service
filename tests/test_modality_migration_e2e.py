import os
import unittest


@unittest.skipUnless(os.environ.get("RUN_DB_E2E") == "1", "실 DB 필요(RUN_DB_E2E=1)")
class TestModalityCanonicalV292(unittest.TestCase):
    """v292 적용 후 asset.modality 가 canonical 5종으로 정규화됐는지 검증(spec 053·FR-301~305).

    선행: ``alembic -c alembic.ini upgrade head`` 로 v292 가 dev DB 에 반영돼 있어야 한다.
    downgrade 왕복(제약 10종 복원·'text' 잔존 0)은 이 apply 단계에서 수동 확인한다
    (레포 관례: 마이그레이션 테스트는 alembic 을 직접 돌리지 않고 적용 후 상태를 단언한다).
    """

    def test_modality_is_canonical_and_no_file_kind_residue(self):
        from dotenv import load_dotenv
        load_dotenv(".env.dev", override=False)
        from src.config.settings import init_settings
        init_settings("dev")
        from src.database.postgres_util import PostgresUtil
        from src.file.file_type_defs import CANONICAL_MODALITIES
        db = PostgresUtil()
        with db:
            with db.transaction() as conn:
                # 1) 저장 modality 는 canonical 5종 부분집합 — file_kind 세분류 잔재 0
                vals = {r[0] for r in conn.execute(
                    "SELECT DISTINCT modality FROM asset").fetchall()}
                self.assertTrue(
                    vals <= set(CANONICAL_MODALITIES),
                    f"canonical 밖 값 잔존: {vals - set(CANONICAL_MODALITIES)}")
                legacy = conn.execute(
                    "SELECT count(*) FROM asset WHERE modality IN "
                    "('txt','pdf','json','word','excel','powerpoint')").fetchone()[0]
                self.assertEqual(legacy, 0, "file_kind 세분류(txt/pdf/json/office) 값이 남아있음")

                # 2) CHECK 제약 정의가 canonical 5종(파일종류 제거)
                chk = conn.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='asset'::regclass AND conname='asset_modality_check'"
                ).fetchone()
                self.assertIsNotNone(chk, "asset_modality_check 제약이 없음(제약명 확인 필요)")
                for m in ("text", "image", "video", "audio", "unknown"):
                    self.assertIn(m, chk[0])
                self.assertNotIn("'txt'", chk[0], "구 file_kind 값이 CHECK 에 남아있음")

    def test_asset_stats_invariant_total_equals_by_modality_sum(self):
        # FR-503: total == sum(by_modality) 이고 modality 는 canonical 부분집합(실 DB)
        from dotenv import load_dotenv
        load_dotenv(".env.dev", override=False)
        from src.config.settings import init_settings
        init_settings("dev")
        from service.portal.asset_stats import asset_stats
        from src.database.postgres_util import PostgresUtil
        from src.file.file_type_defs import CANONICAL_MODALITIES
        db = PostgresUtil()
        with db:
            with db.transaction() as conn:
                s = asset_stats(conn)
                self.assertEqual(
                    s["total"], sum(c["count"] for c in s["by_modality"]),
                    "total 과 by_modality 합 불일치")
                for c in s["by_modality"]:
                    self.assertIn(c["modality"], CANONICAL_MODALITIES)
