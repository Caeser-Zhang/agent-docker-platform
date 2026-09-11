"""Unit tests for the fastk-env drift check that guards the KB whitelist.

Containers provisioned before the whitelist proxy landed point the fastk CLI
straight at the server and carry no proxy token, so ``fastk grep --db <any>``
reaches EVERY database regardless of ``kb_grants`` — a live access-control
bypass. Docker never refreshes the env of an existing container, so such a
container must be detected and recreated. ``_fastk_env_stale`` is that check;
it only reads ``container.attrs`` and decodes the token, so it runs with a
plain fake container object — no Docker daemon required.
"""
from types import SimpleNamespace

from app.config import settings
from app.services import kb_access
from app.services.container_manager import container_manager


def fake_container(env: dict[str, str] | None):
    """A stand-in for a docker-py Container exposing only .attrs["Config"]."""
    config = {} if env is None else {"Env": [f"{k}={v}" for k, v in env.items()]}
    return SimpleNamespace(attrs={"Config": config})


def proxy_env(user_id: str = "u1", **overrides) -> dict[str, str]:
    """The env current provisioning bakes in — the whitelist-respecting shape."""
    env = {
        "FASTDB_BASE_URL": settings.kb_proxy_base,
        "FASTK_API_KEY": kb_access.issue_proxy_token(user_id),
    }
    env.update(overrides)
    return env


def test_legacy_direct_connect_container_is_stale():
    # Exactly the caesar bypass: server URL, baked default db, no proxy token.
    container = fake_container({
        "FASTDB_BASE_URL": "http://host.docker.internal:8000",
        "FASTK_DEFAULT_DB": "global",
    })
    assert container_manager._fastk_env_stale(container) is True


def test_proxy_base_with_baked_default_db_is_stale():
    # Proxy base alone is not enough — a baked-in default predates the whitelist.
    container = fake_container(proxy_env(FASTK_DEFAULT_DB="global"))
    assert container_manager._fastk_env_stale(container) is True


def test_proxy_base_without_token_is_stale():
    container = fake_container({"FASTDB_BASE_URL": settings.kb_proxy_base})
    assert container_manager._fastk_env_stale(container) is True


def test_proxy_base_with_invalid_token_is_stale():
    container = fake_container(proxy_env(FASTK_API_KEY="not-a-real-fernet-token"))
    assert container_manager._fastk_env_stale(container) is True


def test_missing_env_entirely_is_stale():
    assert container_manager._fastk_env_stale(fake_container(None)) is True
    assert container_manager._fastk_env_stale(fake_container({})) is True


def test_current_provisioning_env_is_not_stale():
    # A freshly provisioned container must NOT be flagged, or every start would
    # needlessly recreate it. Any valid proxy token works: the proxy resolves
    # grants live from the token's user id, so re-minting (different Fernet
    # timestamp) still validates.
    container = fake_container(proxy_env("some-other-user"))
    assert container_manager._fastk_env_stale(container) is False
