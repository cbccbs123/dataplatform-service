"""포탈 JWT 단위 테스트 (spec 042)."""
from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

import jwt
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from service.portal.auth import authenticate_token, get_principal, issue_dev_token
from service.portal.auth.verifier import _reset_verifier_for_tests
from src.registry.access_tier import AUTHORIZED, PUBLIC


class PortalAuthTest(unittest.TestCase):
    def setUp(self):
        _reset_verifier_for_tests()
        self._env = mock.patch.dict(
            os.environ,
            {"PORTAL_JWT_SECRET": "test-secret", "PORTAL_AUTH_DISABLED": "0"},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        _reset_verifier_for_tests()

    def test_issue_and_decode_roundtrip(self):
        token = issue_dev_token(user_id="alice")
        p = authenticate_token(token)
        self.assertEqual(p.user_id, "alice")
        self.assertEqual(p.clearance, AUTHORIZED)

    def test_get_principal_requires_token_when_auth_enabled(self):
        with self.assertRaises(HTTPException) as cm:
            get_principal(credentials=None)
        self.assertEqual(cm.exception.status_code, 401)

    def test_get_principal_auth_disabled_anonymous(self):
        with mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "1"}):
            _reset_verifier_for_tests()
            p = get_principal(credentials=None)
        self.assertEqual(p.user_id, "anonymous")
        self.assertEqual(p.clearance, PUBLIC)

    def test_get_principal_auth_disabled_with_bearer(self):
        token = issue_dev_token(user_id="bob")
        with mock.patch.dict(os.environ, {"PORTAL_AUTH_DISABLED": "1"}):
            _reset_verifier_for_tests()
            p = get_principal(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
            )
        self.assertEqual(p.user_id, "bob")
        self.assertEqual(p.clearance, AUTHORIZED)


class PortalAuthHardeningTest(unittest.TestCase):
    """JWT 검증 하드닝 부정 케이스 — exp/sub 필수·issuer 핀 (042 리뷰 후속, 헌법 8조)."""

    _SECRET = "portal-test-secret-32bytes-minimum!"  # RFC 7518 권고 길이(경고 회피)

    def setUp(self):
        _reset_verifier_for_tests()
        self._env = mock.patch.dict(
            os.environ,
            {"PORTAL_JWT_SECRET": self._SECRET, "PORTAL_AUTH_DISABLED": "0"},
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        _reset_verifier_for_tests()

    def _encode(self, payload: dict, *, secret: str | None = None) -> str:
        return jwt.encode(payload, secret or self._SECRET, algorithm="HS256")

    def _future(self) -> datetime:
        return datetime.now(UTC) + timedelta(hours=1)

    def test_expired_token_returns_401(self):
        token = self._encode({"sub": "alice", "exp": datetime.now(UTC) - timedelta(hours=1)})
        with self.assertRaises(HTTPException) as cm:
            authenticate_token(token)
        self.assertEqual(cm.exception.status_code, 401)

    def test_wrong_secret_returns_401(self):
        token = self._encode({"sub": "alice", "exp": self._future()}, secret="another-secret-xx")
        with self.assertRaises(HTTPException) as cm:
            authenticate_token(token)
        self.assertEqual(cm.exception.status_code, 401)

    def test_token_without_exp_returns_401(self):
        # exp 누락 → 영구 유효 토큰 차단(require exp).
        token = self._encode({"sub": "alice"})
        with self.assertRaises(HTTPException) as cm:
            authenticate_token(token)
        self.assertEqual(cm.exception.status_code, 401)

    def test_token_without_sub_returns_401(self):
        token = self._encode({"exp": self._future()})
        with self.assertRaises(HTTPException) as cm:
            authenticate_token(token)
        self.assertEqual(cm.exception.status_code, 401)

    def test_issuer_mismatch_returns_401(self):
        token = self._encode({"sub": "alice", "exp": self._future(), "iss": "evil"})
        with mock.patch.dict(os.environ, {"PORTAL_JWT_ISSUER": "portal"}):
            _reset_verifier_for_tests()
            with self.assertRaises(HTTPException) as cm:
                authenticate_token(token)
        self.assertEqual(cm.exception.status_code, 401)

    def test_issuer_required_when_configured(self):
        # issuer 설정 시 iss claim 없는 토큰도 거부(require iss).
        token = self._encode({"sub": "alice", "exp": self._future()})
        with mock.patch.dict(os.environ, {"PORTAL_JWT_ISSUER": "portal"}):
            _reset_verifier_for_tests()
            with self.assertRaises(HTTPException) as cm:
                authenticate_token(token)
        self.assertEqual(cm.exception.status_code, 401)

    def test_issuer_match_roundtrip(self):
        token = self._encode({"sub": "alice", "exp": self._future(), "iss": "portal"})
        with mock.patch.dict(os.environ, {"PORTAL_JWT_ISSUER": "portal"}):
            _reset_verifier_for_tests()
            p = authenticate_token(token)
        self.assertEqual(p.user_id, "alice")
        self.assertEqual(p.clearance, AUTHORIZED)

    def test_issuer_issued_token_roundtrips(self):
        # issuer 설정 시 dev_issuer 가 iss claim 을 박아 자체 검증을 통과해야 한다.
        with mock.patch.dict(os.environ, {"PORTAL_JWT_ISSUER": "portal"}):
            _reset_verifier_for_tests()
            token = issue_dev_token(user_id="carol")
            p = authenticate_token(token)
        self.assertEqual(p.user_id, "carol")
