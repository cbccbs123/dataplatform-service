"""포탈 다운로드 조회 서비스(``resolve_download_target``/``collect_bundle_assets``/
``build_bundle_zip``) 단위 테스트 (mock conn + 임시파일, DB 불필요).

검증 의도 (plan 010 D-4·D-5)
    - ``resolve_download_target``: registered·비의료면 다운로드 타깃 dict, 그 외/없음 → None(404 게이트).
    - ``collect_bundle_assets``: graph_query(active 양방향, 주입) 이웃 + seed 포함, **결정적 정렬**
      (seed 먼저 → confidence desc → asset_id asc), 이웃 0 → seed 단독(US3 Acc 3),
      ``min_confidence`` 필터·``max_neighbors`` 절단(confidence 상위 N).
    - ``build_bundle_zip``: 존재 파일은 zip 엔트리, **누락은 skip + ``_manifest.json`` 기록**
      (부분 zip, Edge Case), 중복 엔트리명 접미사 구분, 결정적 바이트(헌법 3조).

graph_query 는 download 네임스페이스에서 patch 한다. zip 은 ``zipfile.ZipFile`` 로 재오픈해 검증.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch


def _conn_one(row):
    """``cursor(row_factory=dict_row).fetchone`` 이 ``row`` 를 돌려주는 mock conn."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchone.return_value = row
    conn.cursor.return_value = cur
    return conn, cur


def _conn_paths(path_rows):
    """``cursor(row_factory=dict_row).fetchall`` 이 자산 경로 행들을 돌려주는 mock conn."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.fetchall.return_value = path_rows
    conn.cursor.return_value = cur
    return conn, cur


def _rel(asset_id, confidence):
    """graph_query 반환 형태의 이웃 dict 한 건(필요 키만 충실히)."""
    return {
        "asset_id": asset_id, "kind_code": "duplicate_near", "is_symmetric": True,
        "direction": "undirected", "confidence": confidence, "status": "active",
        "topic": None, "reason": None, "edge_id": f"e-{asset_id}",
    }


class TestResolveDownloadTarget(unittest.TestCase):
    _ROW = {
        "asset_id": "A1", "fs_path": "/data/in/보고서.pdf", "fs_uri": "file:///data/in/보고서.pdf",
        "file_size": 2048, "modality": "text", "domain_label": "general", "status": "registered",
    }

    def test_registered_general_returns_target(self) -> None:
        # registered·비의료 → 다운로드 타깃 dict(file_name = fs_path basename).
        conn, _ = _conn_one(dict(self._ROW))
        from service.portal.download import resolve_download_target

        out = resolve_download_target(conn, asset_id="A1")
        self.assertEqual(out, {
            "asset_id": "A1", "fs_path": "/data/in/보고서.pdf",
            "fs_uri": "file:///data/in/보고서.pdf", "file_size": 2048,
            "modality": "text", "file_name": "보고서.pdf",
        })

    def test_missing_returns_none(self) -> None:
        # 행 없음 → None(404).
        conn, _ = _conn_one(None)
        from service.portal.download import resolve_download_target

        self.assertIsNone(resolve_download_target(conn, asset_id="ZZ"))

    def test_non_registered_returns_none(self) -> None:
        # status != 'registered' → None(404 게이트).
        row = dict(self._ROW)
        row["status"] = "failed"
        conn, _ = _conn_one(row)
        from service.portal.download import resolve_download_target

        self.assertIsNone(resolve_download_target(conn, asset_id="A1"))

    def test_medical_returns_target(self) -> None:
        # 2026-07-23: 도메인 제외 전면 제거 — 의료 자산도 다운로드 타깃이 해소된다(None 아님).
        row = dict(self._ROW)
        row["domain_label"] = "medical"
        conn, _ = _conn_one(row)
        from service.portal.download import resolve_download_target

        self.assertIsNotNone(resolve_download_target(conn, asset_id="A1"))


class TestCollectBundleAssets(unittest.TestCase):
    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_seed_first_then_confidence_desc_then_asset_id_asc(self, mock_rel) -> None:
        # 결정적 정렬: seed 먼저 → confidence desc → 동점은 asset_id asc.
        mock_rel.return_value = [_rel("N3", 0.5), _rel("N1", 0.9), _rel("N2", 0.9)]
        path_rows = [{"asset_id": a, "fs_path": f"/d/{a}.txt"} for a in ("SEED", "N1", "N2", "N3")]
        conn, _ = _conn_paths(path_rows)
        from service.portal.download import collect_bundle_assets

        out = collect_bundle_assets(conn, seed_asset_id="SEED")
        self.assertEqual([t["asset_id"] for t in out], ["SEED", "N1", "N2", "N3"])
        # 각 타깃은 fs_path/file_name 동반.
        self.assertEqual(out[0]["fs_path"], "/d/SEED.txt")
        self.assertEqual(out[1]["file_name"], "N1.txt")

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_no_neighbors_returns_seed_alone(self, mock_rel) -> None:
        # 이웃 0 → seed 단독 1건(US3 Acc 3).
        mock_rel.return_value = []
        conn, _ = _conn_paths([{"asset_id": "SEED", "fs_path": "/d/seed.txt"}])
        from service.portal.download import collect_bundle_assets

        out = collect_bundle_assets(conn, seed_asset_id="SEED")
        self.assertEqual([t["asset_id"] for t in out], ["SEED"])
        self.assertEqual(out[0]["file_name"], "seed.txt")

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_max_neighbors_truncates_to_top_confidence(self, mock_rel) -> None:
        # max_neighbors 초과 시 confidence 상위 N 만 + seed(메가블롭 방지).
        mock_rel.return_value = [_rel("N1", 0.1), _rel("N2", 0.9), _rel("N3", 0.5)]
        path_rows = [{"asset_id": a, "fs_path": f"/d/{a}.txt"} for a in ("SEED", "N1", "N2", "N3")]
        conn, _ = _conn_paths(path_rows)
        from service.portal.download import collect_bundle_assets

        out = collect_bundle_assets(conn, seed_asset_id="SEED", max_neighbors=1)
        self.assertEqual([t["asset_id"] for t in out], ["SEED", "N2"])

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_min_confidence_filters_low_neighbors(self, mock_rel) -> None:
        # min_confidence 미만 이웃 제외.
        mock_rel.return_value = [_rel("N1", 0.1), _rel("N2", 0.9), _rel("N3", 0.5)]
        path_rows = [{"asset_id": a, "fs_path": f"/d/{a}.txt"} for a in ("SEED", "N1", "N2", "N3")]
        conn, _ = _conn_paths(path_rows)
        from service.portal.download import collect_bundle_assets

        out = collect_bundle_assets(conn, seed_asset_id="SEED", min_confidence=0.6)
        self.assertEqual([t["asset_id"] for t in out], ["SEED", "N2"])

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_duplicate_neighbor_edges_collapse_to_one(self, mock_rel) -> None:
        # 같은 이웃이 여러 엣지로 와도 한 번만(최고 confidence 기준).
        mock_rel.return_value = [_rel("N1", 0.3), _rel("N1", 0.8)]
        path_rows = [{"asset_id": a, "fs_path": f"/d/{a}.txt"} for a in ("SEED", "N1")]
        conn, _ = _conn_paths(path_rows)
        from service.portal.download import collect_bundle_assets

        out = collect_bundle_assets(conn, seed_asset_id="SEED")
        self.assertEqual([t["asset_id"] for t in out], ["SEED", "N1"])

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_neighbor_without_asset_row_is_skipped(self, mock_rel) -> None:
        # asset 행이 없는 이웃(파일 경로 모름)은 묶음에서 제외.
        mock_rel.return_value = [_rel("N1", 0.9), _rel("GHOST", 0.8)]
        path_rows = [{"asset_id": "SEED", "fs_path": "/d/seed.txt"},
                     {"asset_id": "N1", "fs_path": "/d/n1.txt"}]
        conn, _ = _conn_paths(path_rows)
        from service.portal.download import collect_bundle_assets

        out = collect_bundle_assets(conn, seed_asset_id="SEED")
        self.assertEqual([t["asset_id"] for t in out], ["SEED", "N1"])

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_non_registered_neighbor_excluded_by_bundle_sql(self, mock_rel) -> None:
        # SQL 게이트: 비registered 이웃은 path 조회에서 제외된다(042 FR-007). 2026-07-23: 도메인 제외 전면 제거.
        mock_rel.return_value = [_rel("N_OK", 0.9), _rel("N_FAIL", 0.8)]
        path_rows = [
            {"asset_id": "SEED", "fs_path": "/d/seed.txt"},
            {"asset_id": "N_OK", "fs_path": "/d/n_ok.txt"},
        ]
        conn, cur = _conn_paths(path_rows)
        from service.portal.download import collect_bundle_assets

        collect_bundle_assets(conn, seed_asset_id="SEED")
        sql = cur.execute.call_args[0][0]
        self.assertIn("registered", sql)
        self.assertNotIn("medical", sql)

    @patch("service.portal.download.fetch_active_relations_for_asset")
    def test_determinism_same_input_same_output(self, mock_rel) -> None:
        # 헌법 3조: 동일 입력 2회 동일 순서.
        from service.portal.download import collect_bundle_assets

        rels = [_rel("N2", 0.9), _rel("N1", 0.9), _rel("N3", 0.5)]
        path_rows = [{"asset_id": a, "fs_path": f"/d/{a}.txt"} for a in ("SEED", "N1", "N2", "N3")]
        mock_rel.return_value = list(rels)
        conn1, _ = _conn_paths([dict(r) for r in path_rows])
        out1 = collect_bundle_assets(conn1, seed_asset_id="SEED")
        mock_rel.return_value = list(rels)
        conn2, _ = _conn_paths([dict(r) for r in path_rows])
        out2 = collect_bundle_assets(conn2, seed_asset_id="SEED")
        self.assertEqual(out1, out2)


class TestBuildBundleZip(unittest.TestCase):
    def test_existing_zipped_missing_recorded_in_manifest(self) -> None:
        # 존재 파일은 엔트리, 누락은 skip + _manifest.json 에 기록(부분 zip, Edge Case).
        from service.portal.download import build_bundle_zip

        with tempfile.TemporaryDirectory() as d:
            p1, p2 = os.path.join(d, "a.txt"), os.path.join(d, "b.txt")
            with open(p1, "wb") as f:
                f.write(b"hello-a")
            with open(p2, "wb") as f:
                f.write(b"hello-b")
            targets = [
                {"asset_id": "A", "fs_path": p1, "file_name": "a.txt"},
                {"asset_id": "B", "fs_path": p2, "file_name": "b.txt"},
                {"asset_id": "MISS", "fs_path": os.path.join(d, "nope.txt"), "file_name": "nope.txt"},
            ]
            data = build_bundle_zip(targets)

        zf = zipfile.ZipFile(io.BytesIO(data))  # 유효 zip 재오픈
        names = set(zf.namelist())
        self.assertIn("a.txt", names)
        self.assertIn("b.txt", names)
        self.assertNotIn("nope.txt", names)  # 누락은 엔트리 없음
        self.assertIn("_manifest.json", names)
        self.assertEqual(zf.read("a.txt"), b"hello-a")
        self.assertEqual(zf.read("b.txt"), b"hello-b")
        manifest = json.loads(zf.read("_manifest.json"))
        self.assertEqual([m["asset_id"] for m in manifest["missing"]], ["MISS"])

    def test_all_present_has_no_manifest(self) -> None:
        # 누락 없으면 manifest 를 만들지 않는다(완전 zip).
        from service.portal.download import build_bundle_zip

        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "only.txt")
            with open(p1, "wb") as f:
                f.write(b"x")
            data = build_bundle_zip([{"asset_id": "A", "fs_path": p1, "file_name": "only.txt"}])

        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertNotIn("_manifest.json", set(zf.namelist()))

    def test_all_missing_returns_zip_with_manifest_only(self) -> None:
        # 전부 누락이어도 전체 실패 금지 — manifest 만 담은 유효 zip.
        from service.portal.download import build_bundle_zip

        data = build_bundle_zip([{"asset_id": "X", "fs_path": "/nope/x.txt", "file_name": "x.txt"}])
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(zf.namelist(), ["_manifest.json"])
        manifest = json.loads(zf.read("_manifest.json"))
        self.assertEqual([m["asset_id"] for m in manifest["missing"]], ["X"])

    def test_duplicate_file_names_disambiguated(self) -> None:
        # 같은 file_name 두 개 → 접미사로 구분, 두 내용 모두 보존.
        from service.portal.download import build_bundle_zip

        with tempfile.TemporaryDirectory() as d:
            p1, p2 = os.path.join(d, "x1"), os.path.join(d, "x2")
            with open(p1, "wb") as f:
                f.write(b"first")
            with open(p2, "wb") as f:
                f.write(b"second")
            targets = [
                {"asset_id": "A", "fs_path": p1, "file_name": "dup.txt"},
                {"asset_id": "B", "fs_path": p2, "file_name": "dup.txt"},
            ]
            data = build_bundle_zip(targets)

        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        self.assertEqual(len(names), 2)
        self.assertIn("dup.txt", names)
        self.assertTrue(any(n != "dup.txt" and n.startswith("dup") for n in names))
        self.assertEqual({zf.read(n) for n in names}, {b"first", b"second"})

    def test_determinism_same_targets_same_bytes(self) -> None:
        # 헌법 3조: 동일 타깃 2회 동일 바이트(zip 타임스탬프 고정).
        from service.portal.download import build_bundle_zip

        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "a.txt")
            with open(p1, "wb") as f:
                f.write(b"hello-a")
            targets = [
                {"asset_id": "A", "fs_path": p1, "file_name": "a.txt"},
                {"asset_id": "MISS", "fs_path": os.path.join(d, "nope.txt"), "file_name": "nope.txt"},
            ]
            self.assertEqual(build_bundle_zip(targets), build_bundle_zip(targets))


if __name__ == "__main__":
    unittest.main()


class TestBuildBundleZipStream(unittest.TestCase):
    """069 T008(P1-2) — zip 스트리밍 코어: bytes 래퍼와 동일 바이트·결정성·부분복사(copyfileobj)."""

    def _targets(self, d: str) -> list[dict]:
        p1 = os.path.join(d, "a.txt")
        with open(p1, "wb") as f:
            f.write(b"hello-stream" * 1000)
        return [
            {"asset_id": "A", "fs_path": p1, "file_name": "a.txt"},
            {"asset_id": "MISS", "fs_path": os.path.join(d, "no.txt"), "file_name": "no.txt"},
        ]

    def test_stream_equals_bytes_wrapper_and_deterministic(self) -> None:
        # 스트림 코어 == bytes 래퍼(하위호환) == 재호출(결정성·1980 타임스탬프·순서 보존).
        from service.portal.download import build_bundle_zip, build_bundle_zip_stream

        with tempfile.TemporaryDirectory() as d:
            targets = self._targets(d)
            with build_bundle_zip_stream(targets) as s:
                b_stream = s.read()
            b_wrap1 = build_bundle_zip(targets)
            b_wrap2 = build_bundle_zip(targets)
        self.assertEqual(b_stream, b_wrap1)   # 코어-래퍼 바이트 동일(기존 소비자 무영향)
        self.assertEqual(b_wrap1, b_wrap2)    # 결정성(동일 입력 2회 동일 바이트)
        zf = zipfile.ZipFile(io.BytesIO(b_stream))
        self.assertEqual(zf.getinfo("a.txt").date_time, (1980, 1, 1, 0, 0, 0))
        self.assertIn("_manifest.json", zf.namelist())

    def test_copies_in_chunks_not_full_read(self) -> None:
        # 원본을 fh.read() 전량 적재하지 않고 copyfileobj(64KiB) 로 흘린다(P1-2 핵심).
        from unittest.mock import patch as _patch

        from service.portal import download as dl

        with tempfile.TemporaryDirectory() as d:
            targets = self._targets(d)[:1]
            with _patch.object(dl.shutil, "copyfileobj", wraps=dl.shutil.copyfileobj) as m_copy:
                with dl.build_bundle_zip_stream(targets) as s:
                    s.read()
            m_copy.assert_called_once()
            self.assertEqual(m_copy.call_args.args[2], 64 * 1024)

    def test_spool_closed_on_midway_error(self) -> None:
        # 리뷰 🟡1 회귀: zip 조립 중 예외(디스크 오류 등) 시 spool 핸들 close 후 재전파(FD 누수 차단).
        from unittest.mock import patch as _patch

        from service.portal import download as dl

        captured: dict = {}
        orig_spool = tempfile.SpooledTemporaryFile

        def capture(*a, **k):
            s = orig_spool(*a, **k)
            captured["s"] = s
            return s

        with tempfile.TemporaryDirectory() as d:
            targets = self._targets(d)[:1]
            with _patch.object(dl.tempfile, "SpooledTemporaryFile", side_effect=capture), \
                 _patch.object(dl.shutil, "copyfileobj", side_effect=OSError("disk fail")):
                with self.assertRaises(OSError):
                    dl.build_bundle_zip_stream(targets)
        self.assertTrue(captured["s"].closed)
