"""Who is asking: verify Keycloak access tokens and turn them into a user with roles.

With `AUTH_REQUIRED=true` every call to the REST API must carry `Authorization: Bearer <access token>` from the realm's
Keycloak. The token's signature is checked against the realm's published keys (so a forged or tampered token fails), and so
are its issuer, expiry and the client it was issued to. Who the caller is comes from the token (`preferred_username`), never
from anything in the request body, and what they may do comes from their realm roles:

    desk-requester   ask for trips and follow their own
    desk-approver    see every trip, and approve, reject or edit bookings

With `AUTH_REQUIRED=false` (unit tests, a quick local run) nothing is verified and the API behaves as it did before Keycloak:
callers name themselves. The A2A endpoint is not covered by this: it is agent-to-agent traffic, which agentgateway can protect
separately.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Header, HTTPException

REQUESTER = os.getenv("ROLE_REQUESTER", "desk-requester")
APPROVER = os.getenv("ROLE_APPROVER", "desk-approver")
DEFAULT_ISSUER = "http://keycloak.lab:8080/realms/ai-lab"
DEFAULT_CLIENTS = ("travel-desk-web", "travel-desk-cli")
WEB_CLIENT = "travel-desk-web"


@dataclass(frozen=True)
class User:
    username: str
    roles: frozenset[str]

    @property
    def is_approver(self) -> bool:
        return APPROVER in self.roles

    @property
    def can_request(self) -> bool:
        return REQUESTER in self.roles or APPROVER in self.roles


class Authenticator:
    def __init__(self, *, enabled: bool, issuer: str = DEFAULT_ISSUER, jwks_url: str | None = None,
                 allowed_clients: tuple[str, ...] = DEFAULT_CLIENTS, key_resolver: Callable[[str], Any] | None = None):
        self.enabled = enabled
        self.issuer = issuer.rstrip("/")
        self.allowed_clients = set(allowed_clients)
        if key_resolver is not None:
            self._key = key_resolver  # tests hand in a resolver that needs no network
        else:
            client = jwt.PyJWKClient(jwks_url or f"{self.issuer}/protocol/openid-connect/certs", cache_keys=True, lifespan=300)
            self._key = lambda token: client.get_signing_key_from_jwt(token).key

    @classmethod
    def from_env(cls) -> "Authenticator":
        return cls(
            enabled=os.getenv("AUTH_REQUIRED", "false").lower() == "true",
            issuer=os.getenv("OIDC_ISSUER", DEFAULT_ISSUER),
            jwks_url=os.getenv("OIDC_JWKS_URL") or None,
            allowed_clients=tuple(c.strip() for c in os.getenv("OIDC_CLIENTS", ",".join(DEFAULT_CLIENTS)).split(",") if c.strip()),
        )

    # ---- verification ----------------------------------------------------------------------------------

    def verify(self, token: str) -> User:
        """The user a valid token belongs to. Raises 401 for anything that is not a good token from this realm."""
        try:
            claims = jwt.decode(token, self._key(token), algorithms=["RS256"], issuer=self.issuer, leeway=5,
                                options={"require": ["exp", "iss"], "verify_aud": False})
        except jwt.ExpiredSignatureError:
            raise HTTPException(401, "the access token has expired; sign in again") from None
        except jwt.PyJWTError as exc:
            raise HTTPException(401, f"invalid access token: {exc}") from None
        if claims.get("azp") not in self.allowed_clients:
            raise HTTPException(401, f"the token was issued to '{claims.get('azp')}', which is not a travel desk client")
        username = claims.get("preferred_username")
        if not username:
            raise HTTPException(401, "the token has no preferred_username")
        return User(username=username, roles=frozenset(claims.get("realm_access", {}).get("roles", [])))

    def current(self, authorization: str | None = Header(default=None)) -> User | None:
        """FastAPI dependency: the caller, or None when authentication is off. A sync function, so FastAPI runs it in a
        thread (fetching the signing keys is a blocking call)."""
        if not self.enabled:
            return None
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "sign in required: send Authorization: Bearer <access token>")
        return self.verify(authorization.split(" ", 1)[1].strip())

    # ---- what the web app needs to sign in -----------------------------------------------------------------

    def public_config(self) -> dict:
        base = f"{self.issuer}/protocol/openid-connect"
        return {"enabled": self.enabled, "issuer": self.issuer, "client_id": WEB_CLIENT, "authorization_endpoint": f"{base}/auth",
                "token_endpoint": f"{base}/token", "end_session_endpoint": f"{base}/logout",
                "roles": {"requester": REQUESTER, "approver": APPROVER}}
