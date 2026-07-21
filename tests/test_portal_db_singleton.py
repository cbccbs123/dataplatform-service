"""069 T007(P1-1) — 포탈 앱수명 DB 싱글턴 + open_pool 더블체크 락. 실DB 0."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from service.api import _infra as papi


class TestPortalDbSingleton(unittest.TestCase):
    def setUp(self) -> None:
        papi._close_db_singleton()  # 전역 싱글턴 테스트 격리

    def tearDown(self) -> None:
        papi._DB_SINGLETON = None  # close() 호출 없이 초기화(모의 객체)

    def test_get_db_returns_same_instance(self) -> None:
        # 핵심(P1-1): 요청마다 새 PostgresUtil 이 아니라 앱 수명 싱글턴 재사용.
        fake = MagicMock()
        with patch("src.database.postgres_util.PostgresUtil", return_value=fake) as mk:
            db1 = papi._get_db()
            db2 = papi._get_db()
        self.assertIs(db1, db2)
        self.assertEqual(mk.call_count, 1)   # 생성 1회
        fake.open_pool.assert_called_once()  # 풀 선개방 1회

    def test_run_in_db_uses_singleton_not_fresh_pool(self) -> None:
        # _run_in_db/_run_in_db_write 가 싱글턴을 경유(요청당 with db: 생성·파괴 제거).
        fake = MagicMock()
        fake.execute_in_transaction.return_value = "ok"
        papi._DB_SINGLETON = fake
        self.assertEqual(papi._run_in_db(lambda c: c), "ok")
        self.assertEqual(papi._run_in_db_write(lambda c: c), "ok")
        kinds = [c.kwargs.get("idempotent") for c in fake.execute_in_transaction.call_args_list]
        self.assertEqual(kinds, [True, False])  # 조회/쓰기 시맨틱 불변

    def test_close_singleton_closes_and_resets(self) -> None:
        # lifespan 종료 훅: close 호출 + 전역 초기화(재기동 대비).
        fake = MagicMock()
        papi._DB_SINGLETON = fake
        papi._close_db_singleton()
        fake.close.assert_called_once()
        self.assertIsNone(papi._DB_SINGLETON)


class TestOpenPoolDoubleCheckedLock(unittest.TestCase):
    def test_concurrent_open_pool_creates_single_pool(self) -> None:
        # P1-1 짝: check-then-set 경합 — 동시 최초 호출 2스레드에서도 ConnectionPool 생성 1회.
        from src.database.postgres_util import PostgresUtil

        db = PostgresUtil(dsn="postgresql://user:pw@localhost:5432/x")
        created = []

        def slow_pool(*a, **k):
            time.sleep(0.05)  # 경합 창을 벌려 락 없으면 2중 생성 재현되게
            created.append(1)
            return MagicMock()

        with patch("psycopg_pool.ConnectionPool", side_effect=slow_pool):
            ts = [threading.Thread(target=db.open_pool) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(created), 1)  # 풀 2중 생성·누수 차단


if __name__ == "__main__":
    unittest.main()
