"""
auth.py — Keycloak JWT validation.
"""

import os
import time
import httpx
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError

KEYCLOAK_INTERNAL_URL = os.getenv("KEYCLOAK_URL", "http://keycloak:8080")
KEYCLOAK_PUBLIC_URL = os.getenv("KEYCLOAK_PUBLIC_URL", KEYCLOAK_INTERNAL_URL)
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM")
KEYCLOAK_CLIENT = os.getenv("KEYCLOAK_CLIENT_ID")

JWKS_URI = f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
ISSUER = f"{KEYCLOAK_PUBLIC_URL}/realms/{KEYCLOAK_REALM}"

bearer = HTTPBearer(auto_error=False)

_jwks_cache = {"keys": None, "fetched_at": 0}
_JWKS_TTL = 3600


def _get_jwks() -> dict:
    now = time.time()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched_at"] < _JWKS_TTL:
        return _jwks_cache["keys"]
    with httpx.Client(timeout=10) as client:
        resp = client.get(JWKS_URI)
        resp.raise_for_status()
        keys = resp.json()
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


def verify_token(token: str) -> dict:
    try:
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        jwks = _get_jwks()
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key:
            raise HTTPException(status_code=401, detail="Signing key not found")
        return jwt.decode(
            token,
            key,
            algorithms=[key.get("alg", "RS256")],
            issuer=ISSUER,
            options={"verify_aud": False, "verify_iss": True, "verify_exp": True},
        )
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> dict:
    if not creds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verify_token(creds.credentials)


def require_role(role: str):
    def _checker(user: dict = Depends(get_current_user)) -> dict:
        roles = user.get("realm_access", {}).get("roles", [])
        if role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' required",
            )
        return user
    return _checker