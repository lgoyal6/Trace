"""Lazy, thread-safe Snowpark Session factory.

Every other module under backend/snowflake/ must call snowflake_available()
before doing work, and get_session() never raises - it returns None if
credentials are absent so the `fake` profile and CI keep working with zero
Snowflake in the environment.

snowflake.snowpark is imported lazily, inside functions, wrapped in
try/except ImportError, so this module always imports cleanly even when the
package isn't installed (Tier-1 tests never need the real dependency).
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("neulit.snowflake.session")

_STATEMENT_TIMEOUT_SECONDS = 30

_lock = threading.Lock()
_session = None  # type: ignore[var-annotated]
_session_init_attempted = False


def _connection_params() -> dict | None:
    account = os.environ.get("SNOWFLAKE_ACCOUNT")
    user = os.environ.get("SNOWFLAKE_USER")
    password = os.environ.get("SNOWFLAKE_PASSWORD")
    if not account or not user or not password:
        return None
    params = {
        "account": account,
        "user": user,
        "password": password,
        "role": os.environ.get("SNOWFLAKE_ROLE", "NEULIT_APP"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "NEULIT_WH"),
        "database": os.environ.get("SNOWFLAKE_DATABASE", "NEULIT"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "CORE"),
    }
    # Accounts with MFA enrolled reject a raw account password for
    # programmatic access ("MFA authentication is required, but none of your
    # current MFA methods are supported for programmatic authentication").
    # The supported paths are a Programmatic Access Token or key-pair. A PAT
    # goes in SNOWFLAKE_PASSWORD as-is; some connector versions additionally
    # want authenticator=PROGRAMMATIC_ACCESS_TOKEN, and `externalbrowser` is
    # useful for local interactive runs -- so this is a passthrough rather
    # than a hardcoded mode. Unset means the connector's default.
    authenticator = os.environ.get("SNOWFLAKE_AUTHENTICATOR", "").strip()
    if authenticator:
        params["authenticator"] = authenticator
    return params


def get_session():
    """Return the process-wide Snowpark Session, creating it on first use.

    Returns None (never raises) if SNOWFLAKE_* credentials are absent, or if
    the snowflake-snowpark-python package is not installed, or if connection
    fails. Callers must treat None as "Snowflake unavailable" and degrade.
    """
    global _session, _session_init_attempted

    if _session is not None:
        return _session

    with _lock:
        if _session is not None:
            return _session
        if _session_init_attempted:
            # Already tried and failed this process lifetime; don't hammer
            # a suspended/unreachable account on every call.
            return None
        _session_init_attempted = True

        params = _connection_params()
        if params is None:
            logger.info("snowflake.session: SNOWFLAKE_* credentials absent, staying degraded")
            return None

        try:
            from snowflake.snowpark import Session
        except ImportError:
            logger.warning("snowflake.session: snowflake-snowpark-python not installed, staying degraded")
            return None

        try:
            session = Session.builder.configs(params).create()
            session.sql(
                f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {_STATEMENT_TIMEOUT_SECONDS}"
            ).collect()
        except Exception:
            logger.exception(
                "snowflake.session: connect failed account=%s warehouse=%s role=%s database=%s schema=%s",
                params["account"], params["warehouse"], params["role"],
                params["database"], params["schema"],
            )
            return None

        logger.info(
            "snowflake.session: connected account=%s warehouse=%s role=%s database=%s schema=%s",
            params["account"], params["warehouse"], params["role"],
            params["database"], params["schema"],
        )
        _session = session
        return _session


def snowflake_available() -> bool:
    """True if a live Snowpark session is (or can be) established."""
    return get_session() is not None


def reset_session_for_tests() -> None:
    """Test-only hook: clears the cached session so tests can mock get_session
    behavior across process-wide lru-style state.
    """
    global _session, _session_init_attempted
    with _lock:
        _session = None
        _session_init_attempted = False
