"""Small, provider-neutral execution guidance for Bridge missions."""

from __future__ import annotations

from typing import Any

DEFAULT_TEAMS_ENABLED = True
DEFAULT_TOKEN_SPEND = "medium"
TOKEN_SPEND_PREFERENCES = ("low", "medium", "high")
POLICY_MARKER = "LLM Bridge execution policy:"

_TEAM_POLICY = (
    "Use native subagents only for useful, separable work; give each a focused brief and only relevant context. "
    "Choose an appropriate supported model, request compact evidence or artifact results, and avoid redundant "
    "investigation and cross-talk. Track every child to a terminal outcome; independently check its findings, "
    "and keep verification bounded. Preserve the user's instructions. If this provider cannot delegate, work "
    "as one agent without claiming otherwise."
)
_TOKEN_SPEND_POLICY = {
    "low": (
        "Minimize extra passes and repeated context; escalate when a lean approach cannot meet the requirements."
    ),
    "medium": (
        "Choose reasoning depth and review effort in proportion to the task's risk and uncertainty."
    ),
    "high": (
        "Use more capable reasoning and independent skeptical checks where they can improve consequential or "
        "uncertain work."
    ),
}
_VALIDATION_POLICY = (
    "Respect explicit model, effort, authorization, and user choices. Never skip mandatory validation or acceptance "
    "requirements for cost."
)


def validate_settings(teams_enabled: Any, token_spend: Any) -> tuple[bool, str]:
    """Validate and normalize settings shared by TOML, CLI, and stored missions."""
    if not isinstance(teams_enabled, bool):
        raise ValueError("Bridge teams setting must be true or false")
    if not isinstance(token_spend, str) or token_spend not in TOKEN_SPEND_PREFERENCES:
        raise ValueError("Bridge token spend preference must be low, medium, or high")
    return teams_enabled, token_spend


def defaults_from_tool_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve defaults for new missions; old missions are handled separately."""
    bridge = config.get("bridge") if isinstance(config, dict) else None
    bridge = bridge if isinstance(bridge, dict) else {}
    teams = bridge.get("teams", DEFAULT_TEAMS_ENABLED)
    token_spend = bridge.get("token_spend", DEFAULT_TOKEN_SPEND)
    teams, token_spend = validate_settings(teams, token_spend)
    return {"team_policy_enabled": teams, "token_spend": token_spend}


def effective_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return resolved settings, preserving pre-policy missions as legacy."""
    config = config if isinstance(config, dict) else {}
    if "team_policy_enabled" not in config and "token_spend" not in config:
        return {"teams_enabled": False, "token_spend": "legacy", "legacy": True}
    teams = config.get("team_policy_enabled", False)
    token_spend = config.get("token_spend", DEFAULT_TOKEN_SPEND)
    try:
        teams, token_spend = validate_settings(teams, token_spend)
    except ValueError:
        # Corrupt or hand-edited old state should not prevent a mission from running.
        teams, token_spend = False, DEFAULT_TOKEN_SPEND
    return {"teams_enabled": teams, "token_spend": token_spend, "legacy": False}


def prompt_prefix(config: dict[str, Any] | None) -> str:
    """Build the single stable policy prefix for every Bridge provider launch."""
    settings = effective_settings(config)
    if settings["legacy"]:
        return ""
    parts = [POLICY_MARKER]
    if settings["teams_enabled"]:
        parts.append(_TEAM_POLICY)
    parts.append(f"Token spend preference ({settings['token_spend']}): {_TOKEN_SPEND_POLICY[settings['token_spend']]}")
    parts.append(_VALIDATION_POLICY)
    return "\n".join(parts) + "\n\n"


def apply_prefix_once(prompt_text: str, config: Any, *, already_applied: bool) -> tuple[str, bool]:
    """Apply Bridge-owned prompt guidance once, independent of user prompt contents."""
    if already_applied:
        return prompt_text, True
    prefix = prompt_prefix(config)
    return (prefix + prompt_text if prefix else prompt_text), bool(prefix)
