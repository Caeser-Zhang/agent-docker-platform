"""User-scoped built-in MCP enable/disable.

The platform-wide switch (host ``opencode.json`` → ``builtin_mcp``) is a floor,
never a ceiling: a user may only *narrow* what their own agent can use, and a
personal choice must never leak into another user's container. These tests
cover the three layers that make that true — the visibility set
(:mod:`app.services.opencode_config`), the persisted preference
(:mod:`app.services.user_config`), the HTTP surface and the runtime push
(:mod:`app.services.visibility`).
"""
import json

import pytest

from app.routers import config as config_router
from app.services import opencode_config, visibility
from app.services import user_config as uc

# Discovery reads /builtin-mcp on the host; every test below pins it so the
# assertions never depend on what happens to be installed there.
BUILTINS = {
    "web_search": {"type": "local", "enabled": True, "command": ["node", "/opt/search.js"]},
    "code_graph": {"type": "local", "enabled": True, "command": ["node", "/opt/graph.js"]},
    "retired_tool": {"type": "local", "enabled": False},
}


@pytest.fixture
def builtins(monkeypatch):
    """Serve BUILTINS from every code path that discovers built-in servers."""
    monkeypatch.setattr(opencode_config, "builtin_mcp_servers", lambda: dict(BUILTINS))
    monkeypatch.setattr(opencode_config, "_discover_builtin_plugins", lambda: [])
    monkeypatch.setattr(opencode_config, "load_source_config", lambda: ({}, "test"))
    return BUILTINS


# ------------------------------------------------------------------
#  Visibility set: platform hides ∪ personal hides
# ------------------------------------------------------------------

def test_user_hidden_layer_unions_with_platform():
    source = {
        "builtin_mcp": {"a": {"enabled": False}},
        "mcp": {"b": {"type": "remote", "enabled": False}},
    }
    assert opencode_config.hidden_mcp_servers(source) == {"a", "b"}
    assert opencode_config.hidden_mcp_servers(source, user_hidden={"c"}) == {"a", "b", "c"}
    # An empty personal layer changes nothing (and must not raise).
    assert opencode_config.hidden_mcp_servers(source, user_hidden=set()) == {"a", "b"}


def test_hidden_mcps_from_config_reads_deny_rules_back():
    doc = {
        "mcp": {
            "web_search": {"type": "local", "enabled": True},
            "jina.ai": {"type": "remote", "enabled": True},
        },
        # "jina.ai" is stored under its sanitized key; an allow rule (an
        # un-hide pushed at runtime) is not a hide.
        "permission": {"web_search_*": "deny", "jina_ai_*": "allow", "skill": {"*": "allow"}},
    }
    assert opencode_config.hidden_mcps_from_config(doc) == {"web_search"}
    assert opencode_config.hidden_mcps_from_config({"mcp": {}, "permission": {}}) == set()
    assert opencode_config.hidden_mcps_from_config({}) == set()


def test_container_config_applies_personal_hide(builtins):
    cfg = opencode_config.build_container_config(user_hidden_mcps={"code_graph"})
    # Hidden → denied; the server itself stays connected so a runtime permission
    # flip (not a container restart) is enough to bring it back.
    assert cfg["permission"]["code_graph_*"] == "deny"
    assert "web_search_*" not in cfg["permission"]
    assert cfg["mcp"]["code_graph"]["enabled"] is True
    # The plugin-config injection path reads the effective set back out of the
    # rendered document, so the two tracks must agree.
    assert opencode_config.hidden_mcps_from_config(cfg) == {"code_graph"}


def test_platform_hide_wins_over_personal_enable(builtins, monkeypatch):
    """A platform-wide hide survives an empty personal layer and a user 'enable'."""
    monkeypatch.setattr(
        opencode_config,
        "load_source_config",
        lambda: ({"builtin_mcp": {"retired_tool": {"enabled": False}}}, "test"),
    )
    assert opencode_config.hidden_mcp_servers(user_hidden=set()) == {"retired_tool"}
    # Users cannot widen: the API rejects it, and even a stray preference row
    # would only ever be unioned into the hidden set.
    cfg = opencode_config.build_container_config(user_hidden_mcps={"web_search"})
    assert cfg["permission"]["retired_tool_*"] == "deny"
    assert cfg["permission"]["web_search_*"] == "deny"


# ------------------------------------------------------------------
#  Persisted preference
# ------------------------------------------------------------------

async def test_toggle_row_disappears_when_inheriting(db_factory):
    async with db_factory() as db:
        assert await uc.list_builtin_toggles(db, "u1") == {}
        assert await uc.user_hidden_builtin_mcps(db, "u1") == set()

        await uc.set_builtin_toggle(db, "u1", "code_graph", False)
        assert await uc.list_builtin_toggles(db, "u1") == {"code_graph": False}
        assert await uc.user_hidden_builtin_mcps(db, "u1") == {"code_graph"}

        # Re-enabling stores nothing: the user inherits the platform default
        # again, so a later platform-wide hide reaches them too.
        await uc.set_builtin_toggle(db, "u1", "code_graph", True)
        assert await uc.list_builtin_toggles(db, "u1") == {}
        assert await uc.user_hidden_builtin_mcps(db, "u1") == set()


async def test_personal_hides_are_isolated_per_user(db_factory):
    async with db_factory() as db:
        await uc.set_builtin_toggle(db, "u1", "web_search", False)
        assert await uc.user_hidden_builtin_mcps(db, "u1") == {"web_search"}
        assert await uc.user_hidden_builtin_mcps(db, "u2") == set()


async def test_user_hidden_mcps_feed_the_injected_config(db_factory, builtins):
    async with db_factory() as db:
        await uc.set_builtin_toggle(db, "u1", "web_search", False)
        hidden = await uc.user_hidden_builtin_mcps(db, "u1")
    cfg = opencode_config.build_container_config(user_hidden_mcps=hidden)
    assert cfg["permission"]["web_search_*"] == "deny"
    assert "code_graph_*" not in cfg["permission"]


# ------------------------------------------------------------------
#  HTTP surface
# ------------------------------------------------------------------

@pytest.fixture
def no_runtime_push(monkeypatch):
    """Stub the single-container push; these tests assert on it separately."""
    calls = []

    async def fake(user_id, name):
        calls.append((user_id, name))
        return True

    monkeypatch.setattr(visibility, "apply_user_mcp_visibility", fake)
    return calls


async def test_builtin_mcp_listing_shows_both_layers(client_factory, builtins, monkeypatch):
    client = await client_factory("u1", "alice")
    async with client:
        r = await client.get("/api/user-config/builtin-mcp")
        assert r.status_code == 200
        entries = {e["name"]: e for e in r.json()["mcp"]}
        assert entries["web_search"] == {
            "name": "web_search",
            "type": "local",
            "platform_enabled": True,
            "my_enabled": True,
            "effective_enabled": True,
        }
        # Platform-hidden: the user's own "on" does not make it effective.
        assert entries["retired_tool"]["platform_enabled"] is False
        assert entries["retired_tool"]["effective_enabled"] is False


async def test_disable_affects_only_the_caller(client_factory, db_factory, builtins, no_runtime_push):
    alice = await client_factory("u1", "alice")
    async with alice:
        r = await alice.patch("/api/user-config/builtin-mcp/web_search", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["my_enabled"] is False
        assert r.json()["effective_enabled"] is False
        assert r.json()["applied"] is True
        assert no_runtime_push == [("u1", "web_search")]

        mine = {e["name"]: e for e in (await alice.get("/api/user-config/builtin-mcp")).json()["mcp"]}
        assert mine["web_search"]["my_enabled"] is False

    bob = await client_factory("u2", "bob")
    async with bob:
        bobs = {e["name"]: e for e in (await bob.get("/api/user-config/builtin-mcp")).json()["mcp"]}
    assert bobs["web_search"]["my_enabled"] is True
    assert bobs["web_search"]["effective_enabled"] is True

    async with db_factory() as db:
        assert await uc.user_hidden_builtin_mcps(db, "u1") == {"web_search"}
        assert await uc.user_hidden_builtin_mcps(db, "u2") == set()


async def test_unknown_or_platform_disabled_server_is_rejected(
    client_factory, builtins, no_runtime_push
):
    client = await client_factory("u1", "alice")
    async with client:
        assert (await client.patch("/api/user-config/builtin-mcp/nope", json={"enabled": False})).status_code == 404
        # Trying to switch on what the admin switched off for everyone.
        r = await client.patch("/api/user-config/builtin-mcp/retired_tool", json={"enabled": True})
        assert r.status_code == 409
        assert no_runtime_push == []
        # …and nothing was persisted, so the user still inherits the default.
        entries = {e["name"]: e for e in (await client.get("/api/user-config/builtin-mcp")).json()["mcp"]}
        assert entries["retired_tool"]["my_enabled"] is True


async def test_config_mcp_my_enabled_tracks_the_user(app_client_factory, db_factory, builtins):
    async with db_factory() as db:
        await uc.set_builtin_toggle(db, "u1", "web_search", False)

    alice = app_client_factory([config_router.router], user_id="u1", role="user")
    async with alice:
        a = (await alice.get("/api/config/mcp")).json()["mcp"]
    bob = app_client_factory([config_router.router], user_id="u2", role="user")
    async with bob:
        b = (await bob.get("/api/config/mcp")).json()["mcp"]

    assert a["web_search"]["my_enabled"] is False
    assert b["web_search"]["my_enabled"] is True
    # Host-declared (platform-added) servers stay admin-only.
    assert set(a) == set(BUILTINS) == set(b)


# ------------------------------------------------------------------
#  Runtime push
# ------------------------------------------------------------------

@pytest.fixture
def live_container(monkeypatch, db_factory):
    """A running agent for every user; records the /global/config patches."""
    patches = []

    async def gate(user_id):
        return True, "pw"

    async def http_request(user_id, method, path, **kwargs):
        assert (method, path) == ("PATCH", "/global/config")
        patches.append((user_id, json.loads(kwargs["raw_body"].decode())))
        return {"status": 200, "body": {}}

    async def no_plugin_file(user_id, hidden_mcps=None):
        return True

    monkeypatch.setattr(visibility.agent_controller, "get_agent_gate", gate)
    monkeypatch.setattr(visibility.tunnel_relay, "http_request", http_request)
    # The plugin-config rewrite has its own tests; keep the volume out of this.
    monkeypatch.setattr(visibility, "_sync_plugin_config", no_plugin_file)
    monkeypatch.setattr(visibility, "async_session", db_factory)
    # Platform hides come from the host config file; this fixture models the
    # "platform says visible" case so the personal layer is the only variable.
    monkeypatch.setattr(
        visibility,
        "hidden_mcp_servers",
        lambda source=None, user_hidden=None: set(user_hidden or set()),
    )
    return patches


async def test_admin_unhide_does_not_reenable_for_a_user_who_opted_out(
    live_container, db_factory, monkeypatch
):
    async with db_factory() as db:
        await uc.set_builtin_toggle(db, "u1", "web_search", False)

    async def running():
        return ["u1", "u2"]

    monkeypatch.setattr(visibility, "_running_agent_user_ids", running)
    summary = await visibility.broadcast_visibility_change("mcp", "web_search", hidden=False)

    assert summary == {"applied": 2, "failed": []}
    by_user = dict(live_container)
    # u1 switched it off themselves: the admin's broadcast must keep them denied…
    assert by_user["u1"] == {"permission": {"web_search_*": "deny"}}
    # …while u2, who inherited the default, gets the allow rule back.
    assert by_user["u2"] == {"permission": {"web_search_*": "allow"}}


async def test_personal_push_touches_one_container_only(live_container, db_factory, monkeypatch):
    pushed = []

    async def record(user_id, kind, name, hidden):
        pushed.append((user_id, kind, name, hidden))
        return True

    async def running():
        raise AssertionError("user-scoped changes must not enumerate containers")

    monkeypatch.setattr(visibility, "_push_to_container", record)
    monkeypatch.setattr(visibility, "_running_agent_user_ids", running)

    assert await visibility.apply_user_mcp_visibility("u1", "code_graph") is True
    assert pushed == [("u1", "mcp", "code_graph", False)]


async def test_effective_hidden_set_merges_platform_and_personal(
    live_container, db_factory, monkeypatch
):
    async with db_factory() as db:
        await uc.set_builtin_toggle(db, "u1", "code_graph", False)

    real = opencode_config.hidden_mcp_servers
    monkeypatch.setattr(
        visibility,
        "hidden_mcp_servers",
        lambda source=None, user_hidden=None: real({"builtin_mcp": {"retired_tool": {"enabled": False}}}, user_hidden=user_hidden),
    )
    assert await visibility._effective_mcp_hidden("u1") == {"retired_tool", "code_graph"}
    assert await visibility._effective_mcp_hidden("u2") == {"retired_tool"}
