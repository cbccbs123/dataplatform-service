"""포탈 인증 설정 fail-fast 단위 테스트 (spec 042)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from service.portal.auth.config import load_portal_auth_config


class PortalAuthConfigTest(unittest.TestCase):
    def test_auth_enabled_requires_secret(self):
        with mock.patch.dict(
            os.environ,
            {"PORTAL_AUTH_DISABLED": "0", "PORTAL_JWT_SECRET": ""},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                load_portal_auth_config()

    def test_auth_disabled_allows_default_secret(self):
        with mock.patch.dict(
            os.environ,
            {"PORTAL_AUTH_DISABLED": "1", "PORTAL_JWT_SECRET": ""},
            clear=False,
        ):
            cfg = load_portal_auth_config()
        self.assertTrue(cfg.auth_disabled)
        self.assertTrue(cfg.jwt_secret)
