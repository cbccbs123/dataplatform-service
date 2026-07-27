"""포탈 인증 공개 API.

    ``config`` / ``verifier`` / ``principal`` / ``dev_issuer`` / ``deps`` / ``schemas``
"""

from service.portal.auth.deps import authenticate_token, get_principal, require_principal
from service.portal.auth.dev_issuer import issue_dev_token
from service.portal.auth.principal import ANONYMOUS, Principal, claims_to_principal

__all__ = [
    "ANONYMOUS",
    "Principal",
    "authenticate_token",
    "claims_to_principal",
    "get_principal",
    "issue_dev_token",
    "require_principal",
]
