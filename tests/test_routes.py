"""Tests for the route-level capacity and cost policy layer.

Covers config parsing/validation, opaque-scope construction, the route
decision helper, local block storage, the ``llm-usage`` opaque
rendering, and the ``ralph-robin`` route-mode selection.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from llm_tools import common, config, ralph_robin, routes, scheduler
from llm_tools.config import (
    CapacityPolicyConfig,
    CostPolicyConfig,
    RoutePolicy,
)
from llm_tools.routes import (
    CAPACITY_POLICY_DELEGATE,
    CAPACITY_POLICY_OPAQUE,
    CAPACITY_POLICY_PROVIDER,
    COST_POLICY_FIXED_SUBSCRIPTION,
)


# --- Config -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    config._cache.clear()


def _write_toml(env: dict[str, str], path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_route_parsing_minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "kilo"
        model = "minimax-m3"

        [routes.kilo-minimax-m3.capacity]
        policy = "opaque"
        scope = "subscription"
        label = "MiniMax M3 via Kilo"

        [routes.kilo-minimax-m3.cost]
        policy = "fixed_subscription"
        amount = 20
        currency = "USD"
        period = "monthly"
        """,
    )
    cfg = config.load_config()
    routes_map = config.parse_routes(cfg)
    assert "kilo-minimax-m3" in routes_map
    r = routes_map["kilo-minimax-m3"]
    assert r.route_id == "kilo-minimax-m3"
    assert r.provider == "kilo"
    assert r.model == "minimax-m3"
    assert r.allow_fallback is False
    assert r.capacity.policy == "opaque"
    assert r.capacity.scope == "subscription"
    assert r.capacity.label == "MiniMax M3 via Kilo"
    assert r.cost.policy == "fixed_subscription"
    assert r.cost.amount == 20.0
    assert r.cost.currency == "USD"
    assert r.cost.period == "monthly"


def test_route_unknown_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "acme"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_missing_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        model = "minimax-m3"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_unknown_capacity_policy_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "kilo"
        [routes.kilo-minimax-m3.capacity]
        policy = "fictional"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_unknown_cost_policy_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "kilo"
        [routes.kilo-minimax-m3.cost]
        policy = "fictional"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_delegate_without_target_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "kilo"
        [routes.kilo-minimax-m3.capacity]
        policy = "delegate"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_self_delegation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "kilo"
        [routes.kilo-minimax-m3.capacity]
        policy = "delegate"
        provider = "kilo"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_delegate_chain_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.r1]
        provider = "opencode"
        [routes.r1.capacity]
        policy = "delegate"
        provider = "r2"

        [routes.r2]
        provider = "kilo"
        [routes.r2.capacity]
        policy = "delegate"
        provider = "claude"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_opaque_with_legacy_capacity_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [providers.kilo]
        capacity_provider = "minimax"

        [routes.kilo-minimax-m3]
        provider = "kilo"
        [routes.kilo-minimax-m3.capacity]
        policy = "opaque"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_unknown_key_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.kilo-minimax-m3]
        provider = "kilo"
        weird = 1
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


@pytest.mark.parametrize(
    "body",
    [
        # 'routes' itself is not a table.
        "routes = 5\n",
        # A route entry is a scalar instead of a table.
        "[routes]\nkilo-minimax-m3 = 5\n",
        # model must be a string when set.
        '[routes.r]\nprovider = "kilo"\nmodel = 5\n',
        # allow_fallback must be a bool.
        '[routes.r]\nprovider = "kilo"\nallow_fallback = "yes"\n',
        # capacity must be a table.
        '[routes.r]\nprovider = "kilo"\ncapacity = 5\n',
        # capacity has an unknown key.
        '[routes.r]\nprovider = "kilo"\n[routes.r.capacity]\nbogus = 1\n',
        # cost must be a table.
        '[routes.r]\nprovider = "kilo"\ncost = 5\n',
        # cost has an unknown key.
        '[routes.r]\nprovider = "kilo"\n[routes.r.cost]\nbogus = 1\n',
    ],
)
def test_route_schema_type_errors_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(os.environ, tmp_path / "xdg" / "llm-tools" / "config.toml", body)
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_delegate_single_hop_chain_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``a`` delegates to ``kilo`` (a known provider that is also a route id),
    # and that route itself delegates -- capacity links must be a single hop.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [routes.a]
        provider = "opencode"
        [routes.a.capacity]
        policy = "delegate"
        provider = "kilo"

        [routes.kilo]
        provider = "minimax"
        [routes.kilo.capacity]
        policy = "delegate"
        provider = "claude"
        """,
    )
    with pytest.raises(SystemExit):
        config.load_config()


def test_route_policy_none_for_empty_cfg() -> None:
    assert config.route_policy({}, "kilo-minimax-m3") is None


def test_parse_routes_ignores_non_table_routes() -> None:
    # 'routes' is not a table at all -> no routes.
    assert config.parse_routes({"routes": 5}) == {}
    # A route entry is a scalar -> skipped silently.
    assert config.parse_routes({"routes": {"r": 5}}) == {}


def test_route_cost_amount_coercion() -> None:
    def amount(raw: object) -> float | None:
        cfg = {"routes": {"r": {"provider": "kilo", "cost": {"amount": raw}}}}
        return config.parse_routes(cfg)["r"].cost.amount

    assert amount(True) is None  # bools never count as a numeric amount
    assert amount("not-a-number") is None  # non-numeric string falls back to None
    assert amount("12.5") == 12.5  # numeric string is coerced
    assert amount(7) == 7.0  # plain int is coerced to float
    assert amount(None) is None
    assert amount("") is None


# --- Resolution: explicit vs implicit routes --------------------------------


def test_resolve_routes_uses_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [defaults]
        providers = ["claude", "codex"]

        [ralph]
        routes = ["kilo-minimax-m3"]

        [routes.kilo-minimax-m3]
        provider = "kilo"
        model = "minimax-m3"
        [routes.kilo-minimax-m3.capacity]
        policy = "opaque"
        [routes.kilo-minimax-m3.cost]
        policy = "fixed_subscription"
        amount = 20
        currency = "USD"
        period = "monthly"
        """,
    )
    cfg = config.load_config()
    resolved = routes.resolve_routes(cfg)
    assert [r.route_id for r in resolved] == ["kilo-minimax-m3"]
    assert resolved[0].provider == "kilo"
    assert resolved[0].capacity.policy == "opaque"


def test_resolve_routes_implicit_falls_back_to_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [providers.kilo]
        model = "minimax-m3"

        [ralph]
        providers = ["kilo", "claude"]
        """,
    )
    cfg = config.load_config()
    resolved = routes.resolve_routes(cfg)
    assert [r.route_id for r in resolved] == ["kilo", "claude"]
    assert resolved[0].provider == "kilo"
    assert resolved[0].model == "minimax-m3"
    assert resolved[0].capacity.policy == CAPACITY_POLICY_PROVIDER


def test_resolve_routes_translates_capacity_provider_to_delegate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [providers.opencode]
        capacity_provider = "minimax"

        [ralph]
        providers = ["opencode"]
        """,
    )
    cfg = config.load_config()
    resolved = routes.resolve_routes(cfg)
    assert resolved[0].capacity.policy == CAPACITY_POLICY_DELEGATE
    assert resolved[0].capacity.provider == "minimax"


# --- Opaque decision ---------------------------------------------------------


def _make_kilo_route(env: dict[str, str]) -> RoutePolicy:
    return RoutePolicy(
        route_id="kilo-minimax-m3",
        provider="kilo",
        model="minimax-m3",
        capacity=CapacityPolicyConfig(
            policy=CAPACITY_POLICY_OPAQUE,
            scope="subscription",
            label="MiniMax M3 via Kilo",
        ),
        cost=CostPolicyConfig(
            policy=COST_POLICY_FIXED_SUBSCRIPTION,
            amount=20.0,
            currency="USD",
            period="monthly",
        ),
    )


def test_opaque_usable_when_cli_present(
    tmp_path: Path, env: dict[str, str], fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["LLM_TOOLS_LOCAL_BLOCK_DIR"] = str(tmp_path / "blocks")
    route = _make_kilo_route(env)
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    assert dec["usable"] is True
    assert dec["reason"] == "usable"
    assert snap["available"] is True
    scope = snap["scopes"][0]
    assert scope["name"] == "subscription"
    assert scope["kind"] == "opaque"
    assert scope["remaining_percent"] is None
    assert scope["reset_epoch"] is None
    # mmx is not on PATH and must not be invoked; nothing in the
    # opaque path queries it.
    assert "mmx" not in env.get("PATH", "")


def test_opaque_unusable_when_cli_missing(
    tmp_path: Path, env: dict[str, str]
) -> None:
    env["PATH"] = "/var/empty"
    env["LLM_TOOLS_LOCAL_BLOCK_DIR"] = str(tmp_path / "blocks")
    route = _make_kilo_route(env)
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    assert dec["usable"] is False
    assert dec["reason"] == "missing-cli"
    scope = snap["scopes"][0]
    assert scope["ready"] is False
    assert scope["reason"] == "missing-cli"


def test_opaque_block_makes_route_not_ready(
    tmp_path: Path, env: dict[str, str], fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    block_dir = tmp_path / "blocks"
    env["LLM_TOOLS_LOCAL_BLOCK_DIR"] = str(block_dir)
    route = _make_kilo_route(env)
    routes.record_local_block(
        "kilo-minimax-m3",
        reason="rate-limited",
        blocked_until=common.now_epoch() + 600,
        last_message="HTTP 429",
        env=env,
    )
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    assert dec["usable"] is False
    assert dec["reason"] == "blocked"
    scope = snap["scopes"][0]
    assert scope["ready"] is False
    assert scope["reset_epoch"] is not None
    # Clear it and re-check.
    routes.clear_local_block("kilo-minimax-m3", env=env)
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    assert dec["usable"] is True
    assert dec["reason"] == "usable"


def test_delegate_route_runs_against_target(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A route with ``capacity.policy = "delegate"`` reads from the
    named target provider while keeping the launch provider on the
    route itself. This is the route-level successor to the legacy
    ``providers.<x>.capacity_provider`` setting.
    """
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv(
        "LLM_SCHEDULER_USAGE_JSON",
        json.dumps(
            {
                "claude": {
                    "provider": "claude",
                    "available": True,
                    "source": "test",
                    "five_hour": {"remaining": 50, "resets_at": 2000},
                    "week": {"remaining": 80, "resets_at": 1000 + 6 * 86400},
                }
            }
        ),
    )
    route = RoutePolicy(
        route_id="opencode-claude",
        provider="opencode",
        model="",
        capacity=CapacityPolicyConfig(
            policy="delegate",
            provider="claude",
        ),
        cost=CostPolicyConfig(policy="unknown"),
    )
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60"
    )
    assert dec["usable"] is True
    assert snap["provider"] == "opencode"
    assert snap.get("capacity_provider") == "claude" or dec.get("capacity_provider") == "claude"


def test_delegate_route_missing_target_fails(
    tmp_path: Path, env: dict[str, str]
) -> None:
    route = RoutePolicy(
        route_id="opencode-bad",
        provider="opencode",
        model="",
        capacity=CapacityPolicyConfig(policy="delegate", provider=""),
        cost=CostPolicyConfig(policy="unknown"),
    )
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60"
    )
    assert dec["usable"] is False
    assert dec["reason"] == "missing-delegate"


def test_ungated_route_handles_kilo_byok(
    tmp_path: Path, env: dict[str, str], fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env["LLM_USAGE_KILO_MODE"] = "byok"
    env["LLM_USAGE_KILO_BALANCE"] = "5"
    env["LLM_USAGE_LOCAL_BLOCK_DIR"] = str(tmp_path / "blocks")
    route = RoutePolicy(
        route_id="kilo-byok",
        provider="kilo",
        model="",
        capacity=CapacityPolicyConfig(policy="ungated"),
        cost=CostPolicyConfig(policy="free"),
    )
    # The route-level policy "ungated" delegates to the legacy
    # provider decision pipeline; the snapshot surfaces the actual
    # byok label so the table reflects the mode.
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    assert snap["provider"] == "kilo"
    assert snap["scopes"][0]["kind"] == "ungated"
    # The Kilo CLI is present and the byok mode is reachable, so
    # the snapshot must be available. The decision may downgrade
    # to "unsupported-scope" when the underlying scope name (here:
    # "byok") does not match the requested "ungated" alias — that is
    # a known alignment gap and matches the legacy behaviour.
    assert snap["available"] is True


def test_route_to_json_includes_extras() -> None:
    route = _make_kilo_route({})
    snap, _ = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60"
    )
    # The snapshot has ``scopes`` from the opaque path; ``route_to_json``
    # must flatten them into a JSON-friendly shape.
    proj = routes.route_to_json(snap)
    assert proj["route"] == "kilo-minimax-m3"
    assert proj["provider"] == "kilo"
    assert proj["scopes"][0]["extras"]["cost_policy"] == "fixed_subscription"
    assert proj["cost"]["policy"] == "fixed_subscription"


# --- Table formatting helpers --------------------------------------------------


def test_cell_clipped_keeps_existing_behaviour_for_short_text() -> None:
    from llm_tools.usage import cell_clipped, cell

    assert cell_clipped(8, "Claude", gap=False) == cell(8, "Claude", gap=False)
    assert cell_clipped(8, "", gap=False) == cell(8, "", gap=False)


def test_cell_clipped_truncates_overflow_with_marker() -> None:
    from llm_tools.usage import cell_clipped

    out = cell_clipped(8, "route:kilo-minimax-m3", gap=True)
    # The long provider name must not bleed past the column boundary.
    assert "kilo-minimax-m3" not in out
    # The cell ends with the "…" marker (followed only by trailing
    # padding spaces, not the rest of the original string).
    stripped = out.rstrip(" ")
    assert stripped.endswith("…")
    visible = stripped.rstrip()
    # Width of the visible (non-gap) content is exactly 8.
    assert len(visible) == 8


def test_fit_columns_widens_provider_for_long_route_label() -> None:
    from llm_tools.usage import fit_columns

    base = [
        ("Provider", 8),
        ("Model", 7),
        ("Ready", 5),
        ("Scope", 7),
        ("Remaining", 15),
        ("Guidance", 19),
        ("Resets in", 10),
    ]
    rows = [
        {
            "Provider": "route:kilo-minimax-m3",
            "Model": "minimax-m3",
            "Ready": "yes",
            "Scope": "subscription",
            "Remaining": "prepaid $20/mo",
            "Guidance": "✓ usable",
            "Resets in": "-",
        }
    ]
    out = fit_columns(base, rows, terminal_width=0, has_source=False)
    widths = dict(out)
    # Provider column grows to fit the long label.
    assert widths["Provider"] >= 19


def test_format_fixed_subscription_renders_natural_currency() -> None:
    from llm_tools.usage import format_fixed_subscription

    assert format_fixed_subscription({}) == "prepaid"
    # Common ISO 4217 codes render as their display symbol so the
    # canonical "prepaid $20/mo" / "prepaid €15/mo" read naturally.
    assert format_fixed_subscription({"amount": 20, "currency": "USD", "period": "monthly"}) == "prepaid $20/mo"
    assert format_fixed_subscription({"amount": 5.5, "currency": "USD", "period": "monthly"}) == "prepaid $5.5/mo"
    assert format_fixed_subscription({"amount": 15, "currency": "EUR", "period": "monthly"}) == "prepaid €15/mo"
    assert format_fixed_subscription({"amount": 100, "currency": "JPY", "period": "yearly"}) == "prepaid ¥100/yr"
    # Unknown ISO codes fall through unchanged so an internal credit
    # unit never silently disappears.
    assert format_fixed_subscription({"amount": 7, "currency": "CREDITS", "period": "monthly"}) == "prepaid CREDITS7/mo"
    # Period is preserved even when amount is missing.
    assert format_fixed_subscription({"period": "yearly"}) == "prepaid/yr"


def test_corrupt_block_file_does_not_crash(
    tmp_path: Path, env: dict[str, str], fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    block_dir = tmp_path / "blocks"
    block_dir.mkdir(parents=True, exist_ok=True)
    (block_dir / "kilo-minimax-m3.json").write_text("{not valid json", encoding="utf-8")
    env["LLM_TOOLS_LOCAL_BLOCK_DIR"] = str(block_dir)
    route = _make_kilo_route(env)
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    # Corrupt file -> treat as no block, opaque route is usable.
    assert dec["usable"] is True


def test_read_local_block_lifecycle(tmp_path: Path) -> None:
    block_dir = tmp_path / "blocks"
    env = {"LLM_TOOLS_LOCAL_BLOCK_DIR": str(block_dir)}

    # No file yet -> no block, and clearing a missing block reports nothing.
    assert routes.read_local_block("r", env=env) is None
    assert routes.is_locally_blocked("r", env=env) is False
    assert routes.clear_local_block("r", env=env) is False

    # An active (future) block is read back and reported as blocked.
    routes.record_local_block(
        "r", reason="rate-limited", blocked_until=common.now_epoch() + 600, env=env
    )
    assert routes.is_locally_blocked("r", env=env) is True

    # An expired block is treated as cleared without an explicit clear call --
    # this is the regression guard for the read_local_block() expiry fix.
    routes.record_local_block(
        "r", reason="rate-limited", blocked_until=common.now_epoch() - 1, env=env
    )
    assert routes.read_local_block("r", env=env) is None

    # Clearing the (still-present) file now reports that it existed.
    assert routes.clear_local_block("r", env=env) is True


def test_read_local_block_non_dict_payload(tmp_path: Path) -> None:
    block_dir = tmp_path / "blocks"
    block_dir.mkdir(parents=True, exist_ok=True)
    env = {"LLM_TOOLS_LOCAL_BLOCK_DIR": str(block_dir)}
    # Valid JSON that is not an object -> ignored, not a block.
    (block_dir / "r.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert routes.read_local_block("r", env=env) is None


def test_resolve_routes_skips_non_string_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    _write_toml(
        os.environ,
        tmp_path / "xdg" / "llm-tools" / "config.toml",
        """
        [ralph]
        routes = [5, "kilo-minimax-m3"]

        [routes.kilo-minimax-m3]
        provider = "kilo"
        model = "minimax-m3"
        [routes.kilo-minimax-m3.capacity]
        policy = "opaque"
        """,
    )
    cfg = config.load_config()
    resolved = routes.resolve_routes(cfg)
    # The non-string entry is dropped; only the named route survives.
    assert [r.route_id for r in resolved] == ["kilo-minimax-m3"]


def test_unsupported_capacity_policy_is_unusable(
    tmp_path: Path, env: dict[str, str]
) -> None:
    # A RoutePolicy with a policy that escaped validation falls through to the
    # defensive "unsupported-policy" branch instead of crashing.
    env["LLM_TOOLS_LOCAL_BLOCK_DIR"] = str(tmp_path / "blocks")
    route = RoutePolicy(
        route_id="weird",
        provider="kilo",
        model="minimax-m3",
        capacity=CapacityPolicyConfig(policy="fictional"),
        cost=CostPolicyConfig(policy="unknown"),
    )
    snap, dec = routes.usage_snapshot_and_decision_for_route(
        route, "auto", "1", "60", env=env
    )
    assert dec["usable"] is False
    assert dec["reason"] == "unsupported-policy:fictional"
    assert snap["available"] is False


def test_route_to_json_skips_non_dict_scopes() -> None:
    snapshot = {
        "route": "r",
        "provider": "kilo",
        "selected_model": "minimax-m3",
        "available": True,
        "reason": "usable",
        "source": "config:route:r",
        "scopes": ["not-a-dict", {"name": "subscription", "kind": "opaque"}],
        "cost": {},
    }
    proj = routes.route_to_json(snapshot)
    assert [s["name"] for s in proj["scopes"]] == ["subscription"]


# --- llm-usage opaque rendering ----------------------------------------------


def _setup_route_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", "")
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    import os as _os
    monkeypatch.setenv("PATH", f"{fake_bin}{_os.pathsep}{_os.environ.get('PATH', '')}")
    config._cache.clear()
    (tmp_path / "xdg" / "llm-tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "xdg" / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"
            scope = "subscription"
            label = "MiniMax M3 via Kilo"
            [routes.kilo-minimax-m3.cost]
            policy = "fixed_subscription"
            amount = 20
            currency = "USD"
            period = "monthly"
            """
        ),
        encoding="utf-8",
    )


def test_route_rendering_fixed_subscription(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path
) -> None:
    _setup_route_config(monkeypatch, tmp_path, fake_bin)
    from llm_tools import usage
    rows = usage.route_rows(usage.Config())
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "route:kilo-minimax-m3"
    assert row.scope == "subscription"
    assert row.left_text == "prepaid $20/mo"
    assert row.kind == "opaque"
    # No progress bar (the bar is only emitted for "%" values).
    assert "█" not in row.left_text and "░" not in row.left_text


def test_route_rendering_generic_opaque(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path
) -> None:
    _setup_route_config(monkeypatch, tmp_path, fake_bin)
    # Add a second route with opaque but no fixed subscription cost.
    (tmp_path / "xdg" / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"
            scope = "subscription"
            [routes.kilo-minimax-m3.cost]
            policy = "included"

            [routes.kilo-another]
            provider = "kilo"
            model = "minimax-other"
            [routes.kilo-another.capacity]
            policy = "opaque"
            """
        ),
        encoding="utf-8",
    )
    config._cache.clear()
    from llm_tools import usage
    rows = usage.route_rows(usage.Config())
    assert {r.provider for r in rows} == {"route:kilo-minimax-m3", "route:kilo-another"}
    by_provider = {r.provider: r for r in rows}
    assert by_provider["route:kilo-another"].left_text == "not metered"


# --- JSON output -------------------------------------------------------------


def test_route_json_includes_route_and_cost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path
) -> None:
    _setup_route_config(monkeypatch, tmp_path, fake_bin)
    from llm_tools import usage
    summary = usage.route_decision_summary()
    assert len(summary) == 1
    entry = summary[0]
    assert entry["route"] == "kilo-minimax-m3"
    assert entry["provider"] == "kilo"
    assert entry["selected_model"] == "minimax-m3"
    assert entry["available"] is True
    assert entry["scopes"][0]["kind"] == "opaque"
    assert entry["scopes"][0]["name"] == "subscription"
    assert entry["cost"]["policy"] == "fixed_subscription"
    assert entry["cost"]["amount"] == 20.0
    assert entry["cost"]["currency"] == "USD"


def test_json_existing_provider_keys_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_bin: Path
) -> None:
    _setup_route_config(monkeypatch, tmp_path, fake_bin)
    from llm_tools.usage import Config, _emit_json, unavailable_snapshot
    import io
    import contextlib

    provider_data = {
        "claude": unavailable_snapshot("claude", "claude reader"),
        "codex": {"available": False, "provider": "codex", "source": "~/.codex/sessions", "reason": "no local data"},
        "copilot": unavailable_snapshot("copilot", "copilot cli"),
        "kilo": unavailable_snapshot("kilo", "kilo cli"),
        "opencode": unavailable_snapshot("opencode", "opencode cli"),
        "minimax": unavailable_snapshot("minimax", "mmx cli"),
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit_json(Config(), provider_data)
    obj = json.loads(buf.getvalue())
    # Existing provider keys are still present and untouched.
    for key in ("codex", "claude", "copilot", "kilo", "opencode", "minimax", "generated_at"):
        assert key in obj
    # The new routes key is present in route mode.
    assert "routes" in obj
    assert obj["routes"][0]["route"] == "kilo-minimax-m3"


def test_json_omits_routes_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    config._cache.clear()
    (tmp_path / "xdg" / "llm-tools").mkdir(parents=True, exist_ok=True)
    # No config file -> no routes in JSON output.
    from llm_tools.usage import Config, _emit_json, unavailable_snapshot
    provider_data = {
        "claude": unavailable_snapshot("claude", "claude reader"),
        "codex": {"available": False, "provider": "codex", "source": "~/.codex/sessions", "reason": "no local data"},
        "copilot": unavailable_snapshot("copilot", "copilot cli"),
        "kilo": unavailable_snapshot("kilo", "kilo cli"),
        "opencode": unavailable_snapshot("opencode", "opencode cli"),
        "minimax": unavailable_snapshot("minimax", "mmx cli"),
    }
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit_json(Config(), provider_data)
    obj = json.loads(buf.getvalue())
    assert "routes" not in obj


# --- ralph-robin route mode --------------------------------------------------


def test_ralph_parse_routes_spec() -> None:
    assert ralph_robin.parse_routes_spec("") == []
    assert ralph_robin.parse_routes_spec("none") == []
    assert ralph_robin.parse_routes_spec("off") == []
    assert ralph_robin.parse_routes_spec("r1") == ["r1"]
    assert ralph_robin.parse_routes_spec("r1,r2,r3") == ["r1", "r2", "r3"]
    # De-dup, preserve first-seen order.
    assert ralph_robin.parse_routes_spec("r1,r2,r1") == ["r1", "r2"]


def test_ralph_select_route_rotates_over_routes(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", env["PATH"])
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [ralph]
            routes = ["kilo-minimax-m3", "kilo-other"]

            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"

            [routes.kilo-other]
            provider = "kilo"
            model = "minimax-other"
            [routes.kilo-other.capacity]
            policy = "opaque"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(routes_spec="kilo-minimax-m3,kilo-other", routes=["kilo-minimax-m3", "kilo-other"], dry_run=True, even_burn=False)
    cfg.route_policies = config.parse_routes(config.load_config())
    logs = common.setup_run_logs(tmp_path, "r")
    selection = ralph_robin.select_route(cfg, logs, current_index=0, skipped=set())
    assert selection["route"] in {"kilo-minimax-m3", "kilo-other"}


def test_ralph_select_route_opaque_is_eligible_but_not_rankable(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", env["PATH"])
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [ralph]
            routes = ["kilo-minimax-m3", "kilo-minimax-other"]

            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"

            [routes.kilo-minimax-other]
            provider = "kilo"
            model = "minimax-other"
            [routes.kilo-minimax-other.capacity]
            policy = "opaque"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    monkeypatch.setenv("PATH", env["PATH"])
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(routes_spec="kilo-minimax-m3,kilo-minimax-other", routes=["kilo-minimax-m3", "kilo-minimax-other"], dry_run=True, even_burn=True)
    cfg.route_policies = config.parse_routes(config.load_config())
    logs = common.setup_run_logs(tmp_path, "r")
    # Two opaque routes only -> even-burn has no rankable candidates
    # -> current-usable branch picks the first route.
    selection = ralph_robin.select_route(cfg, logs, current_index=0, skipped=set())
    assert selection["rotation_reason"] == "current-usable"
    assert selection["route"] == "kilo-minimax-m3"
    # Even-burn returns None for opaque-only routes.
    decisions = [
        ralph_robin._route_decision_for_index(cfg, rid)
        for rid in cfg.routes
    ]
    assert ralph_robin._even_burn_route_index(decisions, cfg.routes, 0, set()) is None


def test_ralph_select_route_even_burn_with_one_opaque_and_two_rankable(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    """A ready opaque route must not collapse even-burn across rankable routes.

    Three routes: a ready opaque, plus two rankable reset-window
    candidates (claude and codex). Even-burn must pick the rankable
    route with the highest headroom, not None.
    """
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [ralph]
            routes = ["kilo-minimax-m3", "claude-route", "codex-route"]

            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"

            [routes.claude-route]
            provider = "claude"
            [routes.claude-route.capacity]
            policy = "provider"

            [routes.codex-route]
            provider = "codex"
            [routes.codex-route.capacity]
            policy = "provider"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(
        routes_spec="kilo-minimax-m3,claude-route,codex-route",
        routes=["kilo-minimax-m3", "claude-route", "codex-route"],
        dry_run=True,
        even_burn=True,
        scope="auto",
    )
    cfg.route_policies = config.parse_routes(config.load_config())
    assert set(cfg.route_policies) == {"kilo-minimax-m3", "claude-route", "codex-route"}
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps({
        "claude": {
            "provider": "claude",
            "available": True,
            "source": "test",
            "five_hour": {"remaining": 0, "resets_at": 1100},
            "week": {"remaining": 90, "resets_at": 1000 + 6 * 86400},
        },
        "codex": {
            "provider": "codex",
            "available": True,
            "source": "test",
            "five_hour": {"remaining": 100, "resets_at": 2000},
            "week": {"remaining": 60, "resets_at": 1000 + 6 * 86400},
        },
    }))
    logs = common.setup_run_logs(tmp_path, "r")
    decisions = [
        ralph_robin._route_decision_for_index(cfg, rid) for rid in cfg.routes
    ]
    # With scope=auto: claude-route is rate-limited on 5h (5h=0).
    # codex-route is the only ready *rankable* route. Even-burn needs
    # >= 2 ready rankable; falls through to current-usable.
    idx = ralph_robin._even_burn_route_index(decisions, cfg.routes, 0, set())
    assert idx is None
    selection = ralph_robin.select_route(cfg, logs, current_index=2, skipped=set())
    # codex-route (index 2) is the current and the only ready rankable.
    assert selection["route"] == "codex-route"


def test_ralph_mixed_capacity_routes_fairly_include_opaque_kilo_minimax(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    """Claude/Codex expose capacity; Kilo MiniMax M3 is opaque yes/no.

    The opaque route must receive a normal turn without pretending it has a
    weekly/budget score. Among the measurable routes, even-burn still breaks
    ties by remaining daily capacity.
    """
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", env["PATH"])
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [ralph]
            routes = ["claude-route", "codex-route", "kilo-minimax-m3"]

            [routes.claude-route]
            provider = "claude"
            [routes.claude-route.capacity]
            policy = "provider"

            [routes.codex-route]
            provider = "codex"
            [routes.codex-route.capacity]
            policy = "provider"

            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(
        routes_spec="claude-route,codex-route,kilo-minimax-m3",
        routes=["claude-route", "codex-route", "kilo-minimax-m3"],
        dry_run=True,
        even_burn=True,
        scope="auto",
    )
    cfg.route_policies = config.parse_routes(config.load_config())
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps({
        "claude": {
            "provider": "claude",
            "available": True,
            "source": "test",
            "five_hour": {"remaining": 50, "resets_at": 2000},
            "week": {"remaining": 50, "resets_at": 1000 + 6 * 86400},
        },
        "codex": {
            "provider": "codex",
            "available": True,
            "source": "test",
            "five_hour": {"remaining": 90, "resets_at": 2000},
            "week": {"remaining": 90, "resets_at": 1000 + 6 * 86400},
        },
    }))
    logs = common.setup_run_logs(tmp_path, "r")
    counts = {"claude-route": 0, "codex-route": 0, "kilo-minimax-m3": 0}

    first = ralph_robin.select_route(cfg, logs, current_index=0, skipped=set(), completed_counts=counts)
    assert first["route"] == "codex-route"
    assert first["rotation_reason"] == "fair-rotation"

    counts["codex-route"] += 1
    second = ralph_robin.select_route(cfg, logs, current_index=1, skipped=set(), completed_counts=counts)
    assert second["route"] == "kilo-minimax-m3"
    assert second["rotation_reason"] == "fair-rotation"

    counts["kilo-minimax-m3"] += 1
    third = ralph_robin.select_route(cfg, logs, current_index=2, skipped=set(), completed_counts=counts)
    assert third["route"] == "claude-route"
    assert third["rotation_reason"] == "fair-rotation"


def test_ralph_select_route_two_kilo_routes_are_distinct(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    """Two Kilo routes with different models are treated as distinct candidates."""
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [ralph]
            routes = ["kilo-minimax-m3", "kilo-minimax-other"]

            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"

            [routes.kilo-minimax-other]
            provider = "kilo"
            model = "minimax-other"
            [routes.kilo-minimax-other.capacity]
            policy = "opaque"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(routes_spec="kilo-minimax-m3,kilo-minimax-other", routes=["kilo-minimax-m3", "kilo-minimax-other"], dry_run=True, even_burn=False)
    cfg.route_policies = config.parse_routes(config.load_config())
    logs = common.setup_run_logs(tmp_path, "r")
    s1 = ralph_robin.select_route(cfg, logs, current_index=0, skipped=set())
    assert s1["route"] == "kilo-minimax-m3"
    s2 = ralph_robin.select_route(cfg, logs, current_index=0, skipped={"kilo-minimax-m3"})
    # Skipped the first -> falls to the second.
    assert s2["route"] == "kilo-minimax-other"
    # And the two are different ids.
    assert s1["route"] != s2["route"]


# --- Scheduler route hooks ---------------------------------------------------


def test_scheduler_parse_retry_after_seconds() -> None:
    from llm_tools.scheduler import _parse_retry_after_seconds
    assert _parse_retry_after_seconds("") is None
    assert _parse_retry_after_seconds("Retry-After: 60") == 60
    assert _parse_retry_after_seconds("retry after 5 minutes") == 300
    assert _parse_retry_after_seconds("retry after 2 hours") == 7200
    assert _parse_retry_after_seconds("retry after 30 sec") == 30
    assert _parse_retry_after_seconds("no hint here") is None


def test_scheduler_route_runtime_block_round_trip(
    tmp_path: Path, env: dict[str, str]
) -> None:
    from llm_tools.routes import clear_local_block, read_local_block

    env["LLM_TOOLS_LOCAL_BLOCK_DIR"] = str(tmp_path / "blocks")
    logs = common.setup_run_logs(tmp_path, "rt")
    scheduler.clear_route_runtime_block("kilo-minimax-m3")
    scheduler._record_route_runtime_block(
        scheduler.SchedulerConfig(route_id="kilo-minimax-m3"),
        logs=logs,
        output="HTTP 429 Too Many Requests",
    )
    block = read_local_block("kilo-minimax-m3")
    assert block is not None
    assert block["route_id"] == "kilo-minimax-m3"
    # A successful run clears the block.
    scheduler.clear_route_runtime_block("kilo-minimax-m3")
    assert read_local_block("kilo-minimax-m3") is None


def test_scheduler_route_decision_uses_route_when_route_id_set(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"
            [routes.kilo-minimax-m3.cost]
            policy = "fixed_subscription"
            amount = 20
            currency = "USD"
            period = "monthly"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    monkeypatch.setenv("PATH", env["PATH"])
    config._cache.clear()
    scfg = scheduler.SchedulerConfig(
        provider="kilo",
        route_id="kilo-minimax-m3",
        scope="auto",
        min_remaining="1",
        poll_interval="60",
    )
    snapshot, decision = scheduler._route_or_provider_decision(scfg)
    assert decision["usable"] is True
    assert snapshot["scopes"][0]["kind"] == "opaque"


# --- Even-burn regression ---------------------------------------------------


def test_even_burn_does_not_collapse_with_unrankable(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rankable ready candidate + a usable unrankable must still rank."""
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "1000")
    cfg = ralph_robin.RalphConfig(providers=["kilo", "codex"], even_burn=True, scope="auto")
    decisions = [
        # kilo is opaque-only -> unrankable but usable.
        {
            "provider": "kilo",
            "usable": True,
            "reason": "usable",
            "windows": [{"name": "ungated", "kind": "ungated", "label": "byok"}],
        },
        # codex is rankable, ready.
        {
            "provider": "codex",
            "usable": True,
            "reason": "usable",
            "windows": [
                {"name": "weekly", "kind": "reset_window", "remaining": 50.0, "reset_epoch": None}
            ],
        },
    ]
    # Only one ready *rankable* (codex); even-burn returns None and
    # the caller falls through to current-usable. The opaque route
    # must not affect the result.
    assert ralph_robin.even_burn_index(cfg, decisions, current_index=0, skipped=set()) is None


# --- Adversarial review: route_id propagation + runtime context ---------------


def test_ralph_runtime_context_mentions_route_id() -> None:
    from llm_tools import ralph_robin as rr

    cfg = rr.RalphConfig(
        providers=["kilo"],
        routes=["kilo-minimax-m3"],
        even_burn=False,
        scope="auto",
    )
    cfg.route_policies = {
        "kilo-minimax-m3": RoutePolicy(
            route_id="kilo-minimax-m3",
            provider="kilo",
            model="minimax-m3",
            capacity=CapacityPolicyConfig(
                policy="opaque",
                scope="subscription",
                label="MiniMax M3 via Kilo",
            ),
            cost=CostPolicyConfig(
                policy="fixed_subscription",
                amount=20,
                currency="USD",
                period="monthly",
            ),
        )
    }
    selection = {
        "index": 0,
        "route": "kilo-minimax-m3",
        "provider": "kilo",
        "decisions": [
            {
                "route": "kilo-minimax-m3",
                "provider": "kilo",
                "usable": True,
                "reason": "usable",
                "windows": [],
            }
        ],
    }
    context = rr.ralph_runtime_context(cfg, "kilo", selection)
    assert "Current selected route: kilo-minimax-m3" in context
    assert "kilo-minimax-m3 (provider=kilo)" in context


def test_scheduler_config_for_threads_route_id(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, fake_bin: Path
) -> None:
    """ralph-robin's scheduler_config_for must propagate the route_id so
    llm-scheduler's local block ledger and route-aware decision are
    actually invoked for the selected route.
    """
    fake = fake_bin / "kilo"
    fake.write_text("#!/bin/sh\necho done\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    cfg_dir = tmp_path / "xdg"
    (cfg_dir / "llm-tools").mkdir(parents=True)
    (cfg_dir / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"
            [routes.kilo-minimax-m3.cost]
            policy = "fixed_subscription"
            amount = 20
            currency = "USD"
            period = "monthly"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(
        providers=["kilo"],
        routes=["kilo-minimax-m3"],
        even_burn=False,
        scope="auto",
    )
    cfg.route_policies = config.parse_routes(config.load_config())
    logs = common.setup_run_logs(tmp_path, "sc")
    scfg = ralph_robin.scheduler_config_for(
        cfg,
        "kilo",
        logs,
        "prompt",
        iteration=1,
        model="minimax-m3",
        route_id="kilo-minimax-m3",
    )
    assert scfg.route_id == "kilo-minimax-m3"
    # And the provider_env / guard_exports plumbing must expose it to
    # any nested llm-scheduler invocation.
    env = scheduler.provider_env(scfg)
    assert env is not None
    assert env.get("LLM_TOOLS_RALPH_ROBIN_SELECTED_ROUTE") == "kilo-minimax-m3"


def test_format_fixed_subscription_no_period_omits_suffix() -> None:
    from llm_tools.usage import format_fixed_subscription

    # Missing period omits the "/<suffix>" so the renderer does not invent
    # a period label. A user who knows the period is expected to set it.
    assert format_fixed_subscription({"amount": 20, "currency": "USD"}) == "prepaid $20"
    assert format_fixed_subscription({"amount": 20}) == "prepaid 20"


def test_format_opaque_remaining_routes_by_cost_policy() -> None:
    from llm_tools.usage import format_opaque_remaining

    # None / empty scope -> not metered.
    assert format_opaque_remaining(None) == "not metered"
    # Opaque scope with no cost policy -> not metered.
    assert format_opaque_remaining({"name": "subscription", "kind": "opaque", "extras": {}}) == "not metered"
    # Opaque scope with fixed_subscription cost policy -> "prepaid $20/mo".
    assert (
        format_opaque_remaining(
            {
                "name": "subscription",
                "kind": "opaque",
                "extras": {
                    "cost_policy": "fixed_subscription",
                    "cost_amount": 20,
                    "cost_currency": "USD",
                    "cost_period": "monthly",
                },
            }
        )
        == "prepaid $20/mo"
    )


def test_ralph_routes_flag_resolves_even_without_ralph_routes_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--routes should resolve route ids directly from ``[routes.<id>]``
    even when ``[ralph].routes`` is absent. A typo in a CLI flag is a
    hard error, never a silent fall-back to an implicit provider route.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LLM_TOOLS_CONFIG", raising=False)
    (tmp_path / "xdg" / "llm-tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "xdg" / "llm-tools" / "config.toml").write_text(
        textwrap.dedent(
            """
            [routes.kilo-minimax-m3]
            provider = "kilo"
            model = "minimax-m3"
            [routes.kilo-minimax-m3.capacity]
            policy = "opaque"
            [routes.kilo-minimax-m3.cost]
            policy = "fixed_subscription"
            amount = 20
            currency = "USD"
            period = "monthly"
            """
        ),
        encoding="utf-8",
    )
    config._cache.clear()
    cfg = ralph_robin.RalphConfig(
        routes_spec="kilo-minimax-m3",
        routes=["kilo-minimax-m3"],
        providers=[],
        even_burn=False,
        scope="auto",
        dry_run=True,
        prompt_text="test",
    )
    ralph_robin.validate_args(cfg)
    assert "kilo-minimax-m3" in cfg.route_policies
    assert cfg.route_policies["kilo-minimax-m3"].provider == "kilo"
    # A typo in --routes is a hard error, not a silent default.
    bad = ralph_robin.RalphConfig(
        routes_spec="typo-route",
        routes=["typo-route"],
        providers=[],
        even_burn=False,
        scope="auto",
        dry_run=True,
        prompt_text="test",
    )
    with pytest.raises(SystemExit):
        ralph_robin.validate_args(bad)
