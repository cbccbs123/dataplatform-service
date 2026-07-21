"""010 G5 — 일반 도메인 포탈 실DB e2e (RUN_DB_E2E=1 일 때만).

검증
    SC-003 (T026): graph_query 양방향 read seam. 대칭 kind 는 ``graph_persist._canonical_pair``
            가 ``(min(node_id), max(node_id))`` 캐논 1행으로 접는다. 그래서 **큰 쪽(dst 로 접힌)
            자산**으로 ``fetch_asset_detail`` 을 부르면, 순진한 ``WHERE src_node = X`` 였다면
            통째로 빠졌을 대칭 이웃이 ``direction='undirected'`` 로 **누락 없이** 나와야 한다.
            작은 쪽(src)으로도 같은 이웃이 나와 양방향이 대칭임을 함께 증명한다.
    SC-004 (T027): 단일 다운로드의 HTTP ``Range`` 부분 요청 바이트 무결성(본문 == 원본 슬라이스)
            + 관계 묶음 zip 이 seed + active 이웃 수만큼 엔트리(누락 0). 2회 호출 동일(결정성·멱등).

설계 메모(driver)
    - 실 dev DB 엔 운영 자산이 많지만, 본 테스트는 임베딩 유사도로 후보를 끌지 않고
      ``sync_graph_edges`` 로 픽스처 자산 2건 사이에 **active 대칭 엣지를 직접 적재**한다
      (auto_approve_min=0.0 → confidence 0.99 가 'active' 로 확정). 운영 자산은 건드리지 않는다.
    - 다운로드 무결성을 위해 fs_path 는 **실제 임시 파일**(``…/portal_e2e/{uuid}.txt`` 마커 경로,
      tearDown 에서 통째로 삭제)을 가리키게 한다. portal_api 다운로드 핸들러는 DB 의 file_size 가
      아니라 디스크 실제 크기(``os.path.getsize``)로 바이트를 산출하므로 무결성 검증이 유효하다.
    - tearDown 의 ``DELETE FROM asset`` 이 node(asset_id ON DELETE CASCADE) → graph_edge
      (src/dst_node ON DELETE CASCADE) → asset_metadata/asset_embedding(CASCADE)로 흘러 테스트가
      만든 흔적을 전부 정리한다(프로덕션 오염 0, FR-014 격리). 임시 파일 디렉터리도 제거한다.
    - **/search 는 e2e 에서 제외**한다 — 검색은 LLM 질의 구조화로 느리고 흔들려 e2e 가 불안정하다
      (016 e2e 교훈과 동일). 검색 결정성·의료배제는 순수/TestClient 단위로 이미 증명됨. 본 e2e 는
      상세(SC-003)·다운로드/묶음(SC-004)에 집중한다.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from dotenv import load_dotenv

_RUN = os.getenv("RUN_DB_E2E") == "1"
_ENV = Path(__file__).resolve().parents[1] / ".env.dev"


def _onehot(idx: int = 1500) -> list[float]:
    """idx 위치만 1.0 인 1536D one-hot 벡터(임베딩 채널 픽스처용)."""
    from src.config.embedding_constants import FIX_EMBEDDING_DIMENSION

    v = [0.0] * FIX_EMBEDDING_DIMENSION
    v[idx] = 1.0
    return v


@unittest.skipUnless(_RUN, "RUN_DB_E2E=1 일 때만")
class TestPortalE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_dotenv(_ENV, override=False)
        # portal_api 다운로드/묶음 핸들러가 자체 PostgresUtil 로 조회 트랜잭션을 열고,
        # 일부 경로가 get_current_settings 를 참조하므로 설정을 초기화한다(009/016 e2e 교훈).
        from src.config.settings import init_settings

        init_settings("dev")
        from src.database.postgres_util import PostgresUtil

        cls.db = PostgresUtil()
        cls.db.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.db.__exit__(None, None, None)

    def setUp(self):
        self._ids: list = []
        # 마커 경로(…/portal_e2e/{uuid}.txt)로 실제 임시 파일을 둔다 — 다운로드 무결성·픽스처 격리.
        self._tmpdir = tempfile.mkdtemp(prefix="portal_e2e_")
        self._marker_dir = os.path.join(self._tmpdir, "portal_e2e")
        os.makedirs(self._marker_dir, exist_ok=True)

    def tearDown(self):
        # 자산 삭제 → node/graph_edge/asset_metadata/asset_embedding ON DELETE CASCADE 로 흔적 정리.
        if self._ids:
            with self.db.transaction() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM asset WHERE asset_id = ANY(%s)", (self._ids,))
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── 픽스처 헬퍼 ──────────────────────────────────────────────────────────
    def _make_portal_asset(
        self,
        *,
        content: bytes,
        core_meta: dict | None = None,
        ext_meta: dict | None = None,
    ) -> str:
        """registered·일반 도메인 자산 1건 적재: 실제 임시 파일 + st 임베딩 1채널.

        fs_path 는 디스크에 실재하는 ``…/portal_e2e/{uuid}.txt`` 로, content 를 그대로 쓴다
        (Range 무결성·묶음 zip 엔트리 검증용). 생성 id 를 self._ids 에 누적해 tearDown 이 정리한다.
        """
        from src.dispatch.types import AssetRecord, EmbeddingItem
        from src.ingest.asset_persist import create_asset, finalize_asset
        from src.ingest.status import AssetStatus, set_status

        fs_path = os.path.join(self._marker_dir, f"{uuid.uuid4().hex}.txt")
        with open(fs_path, "wb") as fh:
            fh.write(content)

        with self.db.transaction() as conn:
            aid = create_asset(
                conn,
                fs_path=fs_path,
                modality="txt",
                file_hash=uuid.uuid4().hex,
                file_size=len(content),
            )
        self._ids.append(aid)
        with self.db.transaction() as conn:
            set_status(conn, aid, AssetStatus.ROUTING)
            set_status(conn, aid, AssetStatus.CLASSIFYING)
            set_status(conn, aid, AssetStatus.EXTRACTING)
        with self.db.transaction() as conn:
            finalize_asset(
                conn,
                aid,
                AssetRecord(
                    core_meta=core_meta or {},
                    ext_meta=ext_meta or {},
                    tags=["portal_e2e"],
                    embeddings=[
                        EmbeddingItem(channel="st", vector=_onehot(), model_name="m")
                    ],
                ),
            )
        return str(aid)

    def _link_symmetric(self, src_id: str, dst_id: str) -> None:
        """src↔dst 를 대칭 kind(same_series)·active 엣지로 직접 적재(캐논 (min,max) 1행)."""
        from src.relations.graph_persist import sync_graph_edges

        with self.db.transaction() as conn:
            sync_graph_edges(
                conn,
                source_asset_id=src_id,
                edges=[
                    {
                        "target_media_item_id": dst_id,
                        "relation_type_code": "same_series",  # is_symmetric=True, status=active
                        "confidence": 0.99,
                        "topic_ko": "포탈",
                        "topic_en": "portal",
                        "subtopic_ko": "",
                        "subtopic_en": "",
                        "reason": "포탈 e2e 대칭 엣지",
                    }
                ],
                allowed_target_ids=frozenset({dst_id}),
                auto_approve_min=0.0,  # 0.99 >= 0.0 → status='active' 강제
            )

    def _node_id(self, asset_id: str) -> str:
        from src.relations.graph_persist import ensure_asset_node

        with self.db.transaction() as conn:
            return ensure_asset_node(conn, asset_id)

    # ── T026: graph_query 양방향 + 상세 (SC-003) ────────────────────────────
    def test_detail_includes_symmetric_neighbor_from_dst_folded_asset(self):
        """SC-003: 대칭 엣지에서 큰 쪽(dst 로 접힌) 자산의 상세에 이웃이 누락 없이 나온다."""
        from service.portal.asset_detail import fetch_asset_detail

        a_id = self._make_portal_asset(
            content=b"A" * 256, core_meta={"src": "portal_e2e_core_a"}, ext_meta={"summary": "자산 A"}
        )
        b_id = self._make_portal_asset(
            content=b"B" * 256, core_meta={"src": "portal_e2e_core_b"}, ext_meta={"summary": "자산 B"}
        )
        self._link_symmetric(a_id, b_id)

        # 캐논 (min,max) 저장이므로 node_id 가 큰 쪽이 dst 로 접힌다 — 그 자산이 핵심 검증 대상.
        node_a, node_b = self._node_id(a_id), self._node_id(b_id)
        if node_a > node_b:
            larger_asset, smaller_asset = a_id, b_id
        else:
            larger_asset, smaller_asset = b_id, a_id

        with self.db.transaction() as conn:
            detail_big = fetch_asset_detail(conn, asset_id=larger_asset)

        self.assertIsNotNone(detail_big, "큰 쪽 자산은 registered·일반이므로 노출돼야 한다")
        # 핵심 단언(ADR): dst 로 접힌 자산도 양방향 seam 으로 대칭 이웃을 끌어온다(누락 0).
        big_relations = detail_big["relations"]
        neighbor_ids = {r["asset_id"] for r in big_relations}
        self.assertIn(
            smaller_asset,
            neighbor_ids,
            "큰 쪽(dst) 자산의 상세에 대칭 이웃이 누락 없이 포함돼야 한다(순진한 WHERE src=X 면 빠짐)",
        )
        sym = next(r for r in big_relations if r["asset_id"] == smaller_asset)
        # FR-201(057): relations 는 이웃 자산 단위로 사전 병합 — kind_code·direction 은 edges 상세에.
        self.assertIn("same_series", sym["kind_codes"], "대칭 kind 가 이웃 kind_codes 에 포함돼야 한다")
        sym_edge = next(e for e in sym["edges"] if e["kind_code"] == "same_series")
        self.assertEqual(sym_edge["direction"], "undirected", "대칭 kind 는 무방향이어야 한다")

        # 작은 쪽(src)으로도 동일 이웃 — 양방향 대칭 확인.
        with self.db.transaction() as conn:
            detail_small = fetch_asset_detail(conn, asset_id=smaller_asset)
        small_neighbors = {r["asset_id"] for r in detail_small["relations"]}
        self.assertIn(larger_asset, small_neighbors, "작은 쪽 자산에서도 이웃이 보여야 한다(대칭)")

        # 상세 계약: core/ext_meta 구분 + 임베딩 채널 요약(원시 벡터 미노출, FR-005).
        self.assertEqual(detail_big["core_meta"], {"src": f"portal_e2e_core_{'a' if larger_asset == a_id else 'b'}"})
        self.assertIn("ext_meta", detail_big)
        self.assertIn("summary", detail_big["ext_meta"])
        channels = detail_big["embedding_channels"]
        st = next(c for c in channels if c["channel"] == "st")
        self.assertEqual(st["chunk_count"], 1, "st 채널 청크 1건 집계")
        # 원시 벡터 키는 어디에도 없어야 한다(FR-005 — count 만 노출).
        self.assertNotIn("embedding", detail_big)
        self.assertNotIn("vector", detail_big)
        for c in channels:
            self.assertEqual(set(c.keys()), {"channel", "chunk_count"})

    # ── T027: 단일 Range 무결성 + 묶음 zip (SC-004) ─────────────────────────
    def test_single_download_range_integrity_and_bundle_zip(self):
        """SC-004: Range 부분 요청 바이트 무결성 + 묶음 zip 엔트리 수(누락 0)·2회 결정성."""
        from fastapi.testclient import TestClient

        from service.api import app

        # 알려진 1000바이트 패턴 — 부분 요청 바이트를 원본 슬라이스와 정확히 비교한다.
        payload = bytes(i % 256 for i in range(1000))
        a_id = self._make_portal_asset(content=payload)
        b_id = self._make_portal_asset(content=bytes((i * 3) % 256 for i in range(500)))
        self._link_symmetric(a_id, b_id)  # seed A 의 active 이웃 = B 1건

        # with 문으로 lifespan(load_dotenv→init_settings) 발화. 핸들러는 자체 PostgresUtil 로 조회.
        with TestClient(app) as client:
            # (1) 전체 다운로드: 200 + 전체 바이트 일치.
            full = client.get(f"/assets/{a_id}/download")
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.content, payload, "전체 다운로드 본문이 원본과 일치해야 한다")

            # (2) Range 부분 요청: 206 + Content-Range + 본문 == 원본[100:200] (무결성, SC-004).
            partial = client.get(
                f"/assets/{a_id}/download", headers={"Range": "bytes=100-199"}
            )
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.headers["content-range"], "bytes 100-199/1000")
            self.assertEqual(
                partial.content, payload[100:200], "부분 요청 본문이 원본 슬라이스와 정확히 일치해야 한다"
            )

            # (3) 묶음 zip: seed(A) + active 이웃(B) = 2 엔트리, 누락 manifest 없음.
            bundle1 = client.get(f"/assets/{a_id}/bundle")
            self.assertEqual(bundle1.status_code, 200)
            self.assertEqual(bundle1.headers["content-type"], "application/zip")
            names1 = zipfile.ZipFile(io.BytesIO(bundle1.content)).namelist()
            self.assertNotIn("_manifest.json", names1, "모든 원본이 존재하므로 누락 manifest 가 없어야 한다")
            self.assertEqual(len(names1), 2, "seed + active 이웃 1건 = zip 엔트리 2개(누락 0)")

            # (4) 2회 호출 결정성: 동일 엔트리 집합 + 동일 바이트(타임스탬프 고정·결정적 정렬).
            bundle2 = client.get(f"/assets/{a_id}/bundle")
            names2 = zipfile.ZipFile(io.BytesIO(bundle2.content)).namelist()
            self.assertEqual(set(names1), set(names2), "2회 호출 동일 엔트리 집합(결정성)")
            self.assertEqual(bundle1.content, bundle2.content, "2회 호출 동일 zip 바이트(결정성)")


if __name__ == "__main__":
    unittest.main()
