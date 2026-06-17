"""GitHub Copilot CLI provider adapter.

Copilot's usable capacity comes from a bounded PTY capture of the CLI's
own footer (``Plan:`` / ``Session:`` lines). The capture is slow, so
``read_copilot`` serves a cached snapshot and revalidates it with a
detached background refresh.
"""

from __future__ import annotations

from typing import Any

from .. import common
from ..capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_COPILOT,
    ProviderSnapshot,
)


def read_copilot_live(env: dict[str, str] | None = None) -> dict[str, Any]:
    return common.read_copilot_live(env)


def read_copilot(env: dict[str, str] | None = None) -> dict[str, Any]:
    return common.read_copilot(env)


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    raw = read_copilot(env)
    if not raw:
        return ProviderSnapshot(
            provider=PROVIDER_COPILOT,
            available=False,
            reason="unavailable",
            source="copilot cli",
        )
    if raw.get("available") is False:
        return ProviderSnapshot(
            provider=PROVIDER_COPILOT,
            available=False,
            reason=str(raw.get("reason", "unavailable")),
            source=raw.get("source", "copilot cli"),
        )
    monthly = raw.get("monthly") if isinstance(raw.get("monthly"), dict) else {}
    reset_epoch = common.copilot_monthly_reset_epoch()
    remaining_percent = monthly.get("remaining")
    scopes = [
        CapacityScope(
            name="monthly",
            kind=CapacityKind.RESET_WINDOW if common.num(remaining_percent) is not None else CapacityKind.UNKNOWN,
            remaining_percent=remaining_percent,
            reset_epoch=reset_epoch,
            resets_at=str(reset_epoch) if reset_epoch is not None else None,
            source=raw.get("source", "copilot cli"),
        )
    ]
    # Additional ("add-on") usage is the dollar amount spent beyond the included
    # credit allowance. The Copilot CLI cannot report it, so it comes from the
    # GitHub billing API and is carried in model_scopes, never consulted by the
    # scheduler. The table renders it as a "spend" row with a left-aligned
    # amount, not as a funded balance that gates readiness.
    model_scopes: list[CapacityScope] = []
    ai_credits = raw.get("ai_credits") if isinstance(raw.get("ai_credits"), dict) else None
    if isinstance(ai_credits, dict) and common.num(ai_credits.get("used")) is not None:
        model_scopes.append(
            CapacityScope(
                name="ai-credits",
                kind=CapacityKind.UNKNOWN,
                remaining_amount=float(common.num(ai_credits.get("used"))),
                source=raw.get("source", "copilot cli"),
                extras={"ai_credits": True},
            )
        )
    addon = common.read_copilot_addon(env)
    if isinstance(addon, dict) and common.num(addon.get("spent")) is not None:
        model_scopes.append(
            CapacityScope(
                name="balance",
                kind=CapacityKind.BALANCE,
                remaining_amount=float(common.num(addon.get("spent"))),
                currency=str(addon.get("currency") or "$"),
                source=str(addon.get("source") or "github billing"),
                extras={"spent": True},
            )
        )
    return ProviderSnapshot(
        provider=PROVIDER_COPILOT,
        available=bool(scopes),
        source=raw.get("source", "copilot cli"),
        scopes=scopes,
        model_scopes=model_scopes,
    )


__all__ = ["PROVIDER_COPILOT", "read", "read_copilot", "read_copilot_live"]
