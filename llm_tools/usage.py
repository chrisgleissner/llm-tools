from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import common


APP_NAME = "llm-usage"
TOOL_COL_WIDTH = 8
TABLE_GAP_WIDTH = 3
SOURCE_COL_WIDTH = 18
PROGRESS_BAR_WIDTH = 10
# Right-aligned "100%" + space + 10-char bar.
REMAINING_COL_WIDTH = PROGRESS_BAR_WIDTH + 1 + 4
GUIDANCE_COL_WIDTH = 19
RESET_COL_WIDTH = 10
GUIDANCE_TOLERANCE_PP = 5.0


USAGE = """Usage: llm-usage [--json] [--watch SECONDS] [--show-copilot-credits] [--statusline] [--no-header] [--log-only]

Shows remaining capacity per scope for:
  - Codex 5-hour window
  - Codex weekly / 7-day window
  - Codex Spark 5-hour and weekly windows
  - Claude Code 5-hour and weekly windows
  - Copilot monthly usage
  - Copilot AI credits (optional, with --show-copilot-credits)
  - Kilo balance, monthly budget, and BYOK/local/ungated state

Options:
  --json              Emit JSON instead of a table.
  -w, --watch SECONDS  Refresh repeatedly.
  --show-copilot-credits  Show Copilot AI credits row.
  --show-source         Show Source column. By default, Source is hidden.
  --hide-source         Hide Source column (default).
  --show-remaining-time  Show Remaining Time column.
  --hide-remaining-time Hide Remaining Time column (default).
  --show-daily-budget   Show Guidance column (default).
  --hide-daily-budget   Hide Guidance column.
  --show-codex-spark    Show Codex Spark rows (default).
  --hide-codex-spark    Hide Codex Spark rows.
  --copilot-monthly-reset-offset-days DAYS  Day offset from month start for Copilot monthly reset (default: 0).
  --statusline        Read Claude Code statusline JSON from stdin, cache it, print compact line.
  --log-only          Sample providers and append to the usage log without printing a table.
  --no-header         Omit table header.
  -h, --help          Show this help.
"""


class Config:
    def __init__(self) -> None:
        env = os.environ
        self.watch_interval = "0"
        self.json_output = False
        self.statusline_mode = False
        self.log_only = False
        self.no_header = False
        self.show_copilot_credits = False
        self.show_source = env.get("LLM_USAGE_SHOW_SOURCE", "0") == "1"
        self.show_remaining_time = env.get("LLM_USAGE_SHOW_REMAINING_TIME", "0") != "0"
        self.show_daily_budget = env.get("LLM_USAGE_SHOW_DAILY_BUDGET", "1") != "0"
        self.show_codex_spark = env.get("LLM_USAGE_SHOW_CODEX_SPARK", "1") != "0"
        self.symbols_enabled = env.get("LLM_TOOLS_NO_SYMBOLS", "0") != "1"
        self.color_enabled = sys.stdout.isatty() and not env.get("LLM_USAGE_NO_COLOR") and env.get("TERM") != "dumb"
        self.terminal_width = terminal_width(env)


@dataclass
class UsageRow:
    provider: str
    scope: str
    remaining: float | None
    left_text: str
    reset: Any
    source: str
    remaining_time: str = "-"
    # Optional secondary fields for non-percent scopes (Kilo balance/ungated).
    amount: float | None = None
    currency: str | None = None
    kind: str | None = None
    label: str | None = None


@dataclass
class GuidanceInfo:
    text: str
    severity: str


def parse_args(argv: list[str]) -> Config:
    cfg = Config()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            cfg.json_output = True
            i += 1
        elif arg in ("-w", "--watch"):
            if i + 1 >= len(argv):
                common.err(f"{arg} requires seconds")
                raise SystemExit(2)
            cfg.watch_interval = argv[i + 1]
            i += 2
        elif arg == "--show-copilot-credits":
            cfg.show_copilot_credits = True
            i += 1
        elif arg == "--show-source":
            cfg.show_source = True
            i += 1
        elif arg == "--hide-source":
            cfg.show_source = False
            i += 1
        elif arg == "--show-remaining-time":
            cfg.show_remaining_time = True
            i += 1
        elif arg == "--hide-remaining-time":
            cfg.show_remaining_time = False
            i += 1
        elif arg == "--show-daily-budget":
            cfg.show_daily_budget = True
            i += 1
        elif arg == "--hide-daily-budget":
            cfg.show_daily_budget = False
            i += 1
        elif arg == "--show-codex-spark":
            cfg.show_codex_spark = True
            i += 1
        elif arg == "--hide-codex-spark":
            cfg.show_codex_spark = False
            i += 1
        elif arg == "--copilot-monthly-reset-offset-days":
            if i + 1 >= len(argv):
                common.err("--copilot-monthly-reset-offset-days requires DAYS")
                raise SystemExit(2)
            os.environ["LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS"] = argv[i + 1]
            i += 2
        elif arg == "--statusline":
            cfg.statusline_mode = True
            i += 1
        elif arg == "--log-only":
            cfg.log_only = True
            i += 1
        elif arg == "--no-header":
            cfg.no_header = True
            i += 1
        elif arg in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        else:
            common.err(f"unknown option: {arg}")
            print(USAGE, end="", file=sys.stderr)
            raise SystemExit(2)
    if not re_int(os.environ.get("LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS", "0"), allow_negative=True):
        common.err("--copilot-monthly-reset-offset-days expects an integer")
        raise SystemExit(2)
    if cfg.watch_interval != "0" and not common.is_number(cfg.watch_interval):
        common.err("--watch requires numeric seconds")
        raise SystemExit(2)
    return cfg


def re_int(value: str, allow_negative: bool = False) -> bool:
    import re

    return bool(re.fullmatch(r"-?[0-9]+" if allow_negative else r"[0-9]+", value or ""))


def terminal_width(env: dict[str, str] | None = None) -> int:
    env = env or os.environ
    try:
        columns = int(env.get("COLUMNS", ""))
    except ValueError:
        columns = 0
    if columns > 0:
        return columns
    return shutil.get_terminal_size((80, 24)).columns


def percent_color_code(integer: int) -> str:
    if integer < 10:
        return "0;31"  # red
    if integer < 30:
        return "0;33"  # yellow
    return "0;32"  # green


def pace_color_code(pace_ratio: float) -> str:
    if pace_ratio < -0.5:
        return "0;31"  # red
    if pace_ratio < -0.15:
        return "0;33"  # yellow/orange
    return "0;32"  # green


def guidance_color_code(info: GuidanceInfo) -> str:
    if info.severity in {"headroom", "lasts"}:
        return "0;36"  # cyan/blue headroom
    if info.severity == "pace":
        return "0;32"  # green target pace
    if info.severity in {"conserve", "runout"}:
        return "0;33"  # yellow/orange over-burn
    if info.severity == "empty":
        return "0;31"  # red over-burn
    return "2;37"  # dim inactive/not applicable


def colorize_percent(value: str, cfg: Config) -> str:
    if not cfg.color_enabled or value in {"-", "unavailable", "unknown", ""}:
        return value
    try:
        integer = int(float(value.rstrip("%")))
    except ValueError:
        return value
    return f"\033[{percent_color_code(integer)}m{value}\033[0m"


def progress_bar(integer: int, width: int = PROGRESS_BAR_WIDTH) -> str:
    filled = max(0, min(width, int(round(integer / 100 * width))))
    return "█" * filled + "░" * (width - filled)


def render_remaining(value: str, cfg: Config) -> str:
    """Render the remaining percentage first, then a compact bar.

    Example: `82% ████████░░`. Non-numeric values ("-", "unavailable",
    "unknown") are passed through unchanged.
    """
    if value in {"-", "unavailable", "unknown", ""} or not value.endswith("%"):
        return value
    try:
        integer = int(float(value.rstrip("%")))
    except ValueError:
        return value
    text = f"{value.rjust(4)} {progress_bar(integer)}"
    if not cfg.color_enabled:
        return text
    return f"\033[{percent_color_code(integer)}m{text}\033[0m"


def window_seconds(window: str) -> float | None:
    if window == "5h":
        return 5 * 3600.0
    if window == "weekly":
        return 7 * 86400.0
    if window == "monthly":
        return common.copilot_monthly_window_days() * 86400.0
    return None


def is_short_window(window: str) -> bool:
    return window == "5h"


def is_budget_window(window: str) -> bool:
    return window in {"weekly", "monthly"}


def expected_remaining_percent(window: str, reset: Any, env: dict[str, str] | None = None) -> float | None:
    duration = window_seconds(window)
    epoch = common.parse_epoch(reset)
    if duration is None or epoch is None:
        return None
    seconds_left = epoch - common.now_epoch(env)
    if seconds_left <= 0:
        return 0.0
    return max(0.0, min(100.0, seconds_left / duration * 100.0))


def row_is_ready(row: UsageRow) -> bool:
    rem = common.num(row.remaining)
    return rem is not None and rem > 0


def tool_ready(rows: list[UsageRow], provider: str) -> bool:
    blocking = [row for row in rows if row.provider == provider and row.scope not in ("ai-credits", "ungated", "byok", "local")]
    return bool(blocking) and all(row_is_ready(row) for row in blocking)


def classify_budget_guidance(window: str, remaining: Any, reset: Any, env: dict[str, str] | None = None) -> GuidanceInfo:
    rem = common.num(remaining)
    expected = expected_remaining_percent(window, reset, env)
    if rem is None or expected is None:
        return GuidanceInfo("· no rate data", "unknown")
    delta = rem - expected
    if delta > GUIDANCE_TOLERANCE_PP:
        return GuidanceInfo("↑ headroom", "headroom")
    if delta < -GUIDANCE_TOLERANCE_PP:
        return GuidanceInfo("↓ conserve", "conserve")
    return GuidanceInfo("= on pace", "pace")


def classify_session_guidance(provider: str, window: str, remaining: Any, reset: Any, env: dict[str, str] | None = None) -> GuidanceInfo:
    rem = common.num(remaining)
    if rem is None or rem <= 0:
        return GuidanceInfo("× empty", "empty")
    epoch = common.parse_epoch(reset)
    if epoch is None:
        return GuidanceInfo("· no rate data", "unknown")
    now = common.now_epoch(env)
    reset_seconds = epoch - now
    if reset_seconds <= 0:
        return GuidanceInfo("✓ lasts until reset", "lasts")
    runout_seconds = common.estimate_remaining_seconds_from_log(provider, window, rem, env)
    if runout_seconds is None:
        return GuidanceInfo("· no rate data", "unknown")
    if runout_seconds < reset_seconds:
        return GuidanceInfo(f"! empty in {common.fmt_duration(runout_seconds)}", "runout")
    return GuidanceInfo("✓ lasts until reset", "lasts")


def classify_guidance(provider: str, window: str, remaining: Any, reset: Any, env: dict[str, str] | None = None) -> GuidanceInfo:
    if is_short_window(window):
        return classify_session_guidance(provider, window, remaining, reset, env)
    if is_budget_window(window):
        return classify_budget_guidance(window, remaining, reset, env)
    return GuidanceInfo("· no rate data", "unknown")


def render_guidance_info(info: GuidanceInfo, cfg: Config) -> str:
    text = info.text
    if not cfg.color_enabled:
        return text
    return f"\033[{guidance_color_code(info)}m{text}\033[0m"


def render_guidance(provider: str, window: str, remaining: Any, reset: Any, cfg: Config) -> str:
    return render_guidance_info(classify_guidance(provider, window, remaining, reset), cfg)


def classify_delta(delta_pp: float | None) -> GuidanceInfo:
    if delta_pp is None:
        return GuidanceInfo("· no rate data", "unknown")
    if delta_pp > GUIDANCE_TOLERANCE_PP:
        return GuidanceInfo("↑ headroom", "headroom")
    if delta_pp < -GUIDANCE_TOLERANCE_PP:
        return GuidanceInfo("↓ conserve", "conserve")
    return GuidanceInfo("= on pace", "pace")


def classify_pace(window: str, remaining: Any, reset: Any, env: dict[str, str] | None = None) -> GuidanceInfo:
    return classify_guidance("", window, remaining, reset, env)


def render_daily_budget(value: float | None, cfg: Config, target: float | None = None) -> str:
    if value is None:
        return render_guidance_info(GuidanceInfo("· no rate data", "unknown"), cfg)
    delta = None if target in (None, 0) else value - target
    return render_guidance_info(classify_delta(delta), cfg)


def render_gate(value: float | None, cfg: Config) -> str:
    return render_ready(value, cfg)


def render_pace_or_gate(window: str, value: float | None, cfg: Config) -> str:
    return render_guidance("", window, value, None, cfg)


def render_pace(window: str, remaining: Any, reset: Any, cfg: Config) -> str:
    return render_guidance("", window, remaining, reset, cfg)


def render_ready(remaining: Any, cfg: Config) -> str:
    rem = common.num(remaining)
    ready = rem is not None and rem > 0
    text = "yes" if ready else "no"
    if not cfg.color_enabled or ready:
        return text
    return f"\033[1;31m{text}\033[0m"


def visible_len(text: str) -> int:
    import re
    import unicodedata

    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    width = 0
    for char in plain:
        if unicodedata.combining(char):
            continue
        category = unicodedata.category(char)
        if category.startswith("C"):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def cell(width: int, text: str, gap: bool = False) -> str:
    pad = max(0, width - visible_len(text))
    return text + (" " * pad) + (" " * TABLE_GAP_WIDTH if gap else "")


def rule(width: int, gap: bool = False, char: str = "-") -> str:
    return char * width + (" " * TABLE_GAP_WIDTH if gap else "")


def format_tool_name(name: str) -> str:
    if name == "GPT-5.3-Codex-Spark" or ("GPT-5.3" in name and "Codex" in name and "Spark" in name):
        return "GPT-5.3 Spark"
    return name


def table_columns(cfg: Config) -> list[tuple[str, int]]:
    cols = [("Tool", TOOL_COL_WIDTH), ("Ready", 5), ("Scope", 7), ("Remaining", REMAINING_COL_WIDTH)]
    if cfg.show_daily_budget:
        cols.append(("Guidance", GUIDANCE_COL_WIDTH))
    if cfg.show_remaining_time:
        cols.append(("Remaining Time", 14))
    cols.append(("Resets in", RESET_COL_WIDTH))
    return cols


def title_separator(cfg: Config) -> str:
    return "·" if cfg.symbols_enabled else "-"


def print_dashboard_header(cfg: Config) -> None:
    stamp = datetime.now().strftime("%H:%M")
    print(f"LLM Usage {title_separator(cfg)} {stamp}")
    print()
    if cfg.show_daily_budget:
        print("Bars: █ available · ░ spent")
        print("Guidance: 5h rows forecast runout; weekly/monthly/budget rows compare remaining quota to time left.")
        print("          ✓ lasts until reset · ! empty before reset · × empty · ↑ headroom · = on pace · ↓ conserve")
        print()


def print_table_header(cfg: Config) -> None:
    cols = table_columns(cfg)
    head, rule_parts = [], []
    last = len(cols) - 1
    line = "─" if cfg.symbols_enabled else "-"
    for idx, (label, width) in enumerate(cols):
        gap = idx != last or cfg.show_source
        head.append(cell(width, label, gap))
        rule_parts.append(rule(width, gap, line))
    if cfg.show_source:
        head.append(cell(SOURCE_COL_WIDTH, "Source"))
        rule_parts.append(rule(SOURCE_COL_WIDTH, False, line))
    print("".join(head))
    print("".join(rule_parts))


def table_fixed_width(cfg: Config) -> int:
    cols = table_columns(cfg)
    width = sum(col_width for _, col_width in cols)
    width += TABLE_GAP_WIDTH * (len(cols) - 1)
    if cfg.show_source:
        width += TABLE_GAP_WIDTH + SOURCE_COL_WIDTH
    return width


def print_provider_separator(cfg: Config, label: str, leading_blank: bool = True) -> None:
    line = "─" if cfg.symbols_enabled else "-"
    if leading_blank:
        print()
    left = f"{line * 2} {label} "
    width = max(table_fixed_width(cfg), len(left) + 8)
    print(left + (line * (width - visible_len(left))))


def format_reset(reset: Any, cfg: Config) -> str:
    epoch = common.parse_epoch(reset)
    if epoch is None:
        return "-"
    total = max(0, epoch - common.now_epoch())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def row_left_text(remaining: float | None, fallback: str = "-") -> str:
    if remaining is None:
        return fallback
    return common.fmt_pct(remaining) + "%"


def row_from_used(cfg: Config, provider: str, window: str, used: Any, reset: Any, source: str, display_provider: str | None = None) -> UsageRow:
    remaining = common.remaining_from_used(used)
    remaining_time = common.estimate_remaining_time_from_log(provider, window, remaining) if cfg.show_remaining_time else "-"
    return UsageRow(display_provider or provider, window, remaining, row_left_text(remaining), reset, source, remaining_time)


def unavailable_rows(provider: str) -> list[UsageRow]:
    return [
        UsageRow(provider, "5h", None, "-", None, "no local data"),
        UsageRow(provider, "weekly", None, "-", None, "no local data"),
    ]


def print_value_row(cfg: Config, provider: str, window: str, remaining: str, remaining_time: str, reset_text: str, time_to_reset: str, source: str, daily_value: float | None = None) -> None:
    rem = common.num(remaining.rstrip("%")) if isinstance(remaining, str) and remaining.endswith("%") else None
    reset = None if time_to_reset == "-" else reset_text
    row = UsageRow(provider=provider, scope=window, remaining=rem, left_text=remaining, reset=reset, source=source, remaining_time=remaining_time or "-")
    print_usage_rows(cfg, [row])


def row_values(cfg: Config, row: UsageRow, display_provider: str, ready_text: str) -> dict[str, str]:
    values = {
        "Tool": display_provider,
        "Ready": ready_text,
        "Scope": row.scope,
        "Remaining": render_remaining(row.left_text, cfg),
        "Guidance": render_guidance(row.provider, row.scope, row.remaining, row.reset, cfg),
        "Remaining Time": row.remaining_time,
        "Resets in": format_reset(row.reset, cfg),
    }
    return values


def print_usage_rows(cfg: Config, rows: list[UsageRow]) -> None:
    cols = table_columns(cfg)
    last = len(cols) - 1
    previous_provider = ""
    for row in rows:
        if previous_provider and row.provider != previous_provider:
            print()
        display_provider = row.provider if row.provider != previous_provider else ""
        ready_text = render_ready(1 if tool_ready(rows, row.provider) else 0, cfg) if display_provider else ""
        previous_provider = row.provider
        values = row_values(cfg, row, display_provider, ready_text)
        parts = []
        for idx, (label, width) in enumerate(cols):
            gap = idx != last or cfg.show_source
            parts.append(cell(width, values.get(label, ""), gap))
        if cfg.show_source:
            parts.append(row.source or "-")
        print("".join(parts))


def print_row(cfg: Config, provider: str, window: str, used: Any, reset: Any, source: str, display_provider: str | None = None) -> None:
    print_usage_rows(cfg, [row_from_used(cfg, provider, window, used, reset, source, display_provider)])


def log_and_print_row(cfg: Config, provider: str, window: str, used: Any, reset: Any, source: str, display_provider: str | None = None) -> None:
    remaining = common.remaining_from_used(used)
    common.log_usage_sample(provider, window, remaining)
    print_row(cfg, provider, window, used, reset, source, display_provider)


def print_unavailable_rows(cfg: Config, provider: str) -> None:
    print_usage_rows(cfg, unavailable_rows(provider))


def codex_rows(cfg: Config, codex_json: dict[str, Any] | None) -> list[UsageRow]:
    if not codex_json:
        return unavailable_rows("Codex")
    rows = codex_json.get("rows") if isinstance(codex_json.get("rows"), list) else []
    if not rows:
        source = codex_json.get("source", "")
        five_used = (codex_json.get("five_hour") or {}).get("used")
        week_used = (codex_json.get("week") or {}).get("used")
        five_remaining = common.remaining_from_used(five_used)
        week_remaining = common.remaining_from_used(week_used)
        common.log_usage_sample("Codex", "5h", five_remaining)
        common.log_usage_sample("Codex", "weekly", week_remaining)
        return [
            row_from_used(cfg, "Codex", "5h", five_used, (codex_json.get("five_hour") or {}).get("resets_at"), source),
            row_from_used(cfg, "Codex", "weekly", week_used, (codex_json.get("week") or {}).get("resets_at"), source),
        ]
    out: list[UsageRow] = []
    for row in rows:
        key = row.get("key", "codex")
        provider = row.get("name", "Codex")
        is_spark = key == "codex-spark" or "spark" in provider.lower()
        if is_spark and not cfg.show_codex_spark:
            continue
        display_provider = format_tool_name(provider)
        source = row.get("source") or codex_json.get("source", "")
        five_used = (row.get("five_hour") or {}).get("used")
        week_used = (row.get("week") or {}).get("used")
        common.log_usage_sample(provider, "5h", common.remaining_from_used(five_used))
        common.log_usage_sample(provider, "weekly", common.remaining_from_used(week_used))
        out.append(row_from_used(cfg, provider, "5h", five_used, (row.get("five_hour") or {}).get("resets_at"), source, display_provider))
        out.append(row_from_used(cfg, provider, "weekly", week_used, (row.get("week") or {}).get("resets_at"), source, display_provider))
    return out


def print_codex_rows(cfg: Config, codex_json: dict[str, Any] | None) -> None:
    print_usage_rows(cfg, codex_rows(cfg, codex_json))


def copilot_rows(cfg: Config, copilot_json: dict[str, Any] | None) -> list[UsageRow]:
    reset_epoch = common.copilot_monthly_reset_epoch()
    if not copilot_json:
        rows = [UsageRow("Copilot", "monthly", None, "unavailable", reset_epoch, "copilot cli")]
        if cfg.show_copilot_credits:
            rows.append(UsageRow("Copilot", "ai-credits", None, "unavailable", None, "copilot cli"))
        return rows
    source = copilot_json.get("source", "copilot cli")
    if copilot_json.get("available") is False:
        rows = [UsageRow("Copilot", "monthly", None, "unavailable", reset_epoch, source)]
        if cfg.show_copilot_credits:
            rows.append(UsageRow("Copilot", "ai-credits", None, "unavailable", None, source))
        return rows
    monthly = copilot_json.get("monthly") if isinstance(copilot_json.get("monthly"), dict) else {}
    monthly_remaining = monthly.get("remaining")
    if monthly_remaining is None:
        monthly_text = "unavailable"
        remaining = None
    else:
        remaining = common.num(monthly_remaining)
        monthly_text = row_left_text(remaining, "unavailable")
        common.log_usage_sample("copilot", "monthly", remaining)
    remaining_time = common.estimate_remaining_time_from_log("copilot", "monthly", monthly_remaining) if cfg.show_remaining_time else ""
    rows = [UsageRow("Copilot", "monthly", remaining, monthly_text, reset_epoch, source, remaining_time or "-")]
    if cfg.show_copilot_credits:
        ai = copilot_json.get("ai_credits") if isinstance(copilot_json.get("ai_credits"), dict) else {}
        ai_text = common.fmt_pct(ai.get("used")) if ai.get("used") is not None else "unknown"
        rows.append(UsageRow("Copilot", "ai-credits", None, ai_text, None, source))
    return rows


def print_copilot_rows(cfg: Config, copilot_json: dict[str, Any] | None) -> None:
    print_usage_rows(cfg, copilot_rows(cfg, copilot_json))


def kilo_rows(cfg: Config, kilo_json: dict[str, Any] | None) -> list[UsageRow]:
    """Render Kilo scopes into a flat list of table rows.

    Kilo does not have session windows: its scopes are balance, budget, and
    (optionally) byok/local/ungated. Each scope becomes its own row with a
    ``scope`` name that the table renders in the Scope column.
    """
    from .providers import kilo_min_balance, kilo_currency
    from .capacity import CapacityKind

    if not kilo_json:
        return [UsageRow("Kilo", "balance", None, "unavailable", None, "kilo cli")]
    source = kilo_json.get("source", "kilo cli")
    if kilo_json.get("available") is False:
        reason = kilo_json.get("reason") or "unavailable"
        rows: list[UsageRow] = []
        # Show one row for the most informative scope (balance when not
        # configured, otherwise the first known scope) so the user sees why
        # Kilo is currently unavailable.
        rows.append(UsageRow("Kilo", "balance", None, reason, None, source))
        return rows
    scopes = kilo_json.get("scopes") if isinstance(kilo_json.get("scopes"), list) else []
    rows = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        name = str(scope.get("name", "?"))
        kind = str(scope.get("kind", ""))
        if kind == CapacityKind.UNGATED:
            label = scope.get("label") or name
            rows.append(
                UsageRow(
                    "Kilo",
                    name,
                    None,
                    str(label),
                    None,
                    source,
                    "-",
                    kind=kind,
                    label=label,
                )
            )
            continue
        if kind == CapacityKind.BALANCE:
            amount = scope.get("remaining_amount")
            currency = scope.get("currency")
            text = format_balance(amount, currency)
            rows.append(
                UsageRow(
                    "Kilo",
                    "balance",
                    None,
                    text,
                    None,
                    source,
                    "-",
                    amount=amount,
                    currency=currency,
                    kind=kind,
                )
            )
            continue
        if kind == CapacityKind.BUDGET:
            rem = scope.get("remaining_percent")
            total = scope.get("total_amount")
            currency = scope.get("currency")
            reset = scope.get("reset_epoch")
            if rem is None:
                text = "unknown"
            else:
                text = row_left_text(rem)
            remaining_time = common.estimate_remaining_time_from_log("Kilo", "budget", rem) if cfg.show_remaining_time else "-"
            rows.append(
                UsageRow(
                    "Kilo",
                    "budget",
                    rem,
                    text,
                    reset,
                    source,
                    remaining_time or "-",
                    amount=scope.get("remaining_amount"),
                    currency=currency,
                    kind=kind,
                )
            )
            continue
    if not rows:
        rows.append(UsageRow("Kilo", "balance", None, "unavailable", None, source))
    return rows


def format_balance(amount: float | None, currency: str | None) -> str:
    if amount is None:
        return "-"
    text = common.fmt_number(amount)
    if currency:
        return f"{currency}{text}"
    return text


def print_kilo_rows(cfg: Config, kilo_json: dict[str, Any] | None) -> None:
    print_usage_rows(cfg, kilo_rows(cfg, kilo_json))


def render_once(cfg: Config) -> None:
    codex = common.read_codex()
    claude = common.read_claude()
    copilot = common.read_copilot()
    from .providers import read_kilo

    kilo = read_kilo()
    if cfg.json_output:
        obj = {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "codex": common.json_for_provider(codex, "codex"),
            "claude": common.json_for_provider(claude, "claude"),
            "copilot": common.json_for_copilot(copilot, cfg.show_copilot_credits),
            "kilo": _kilo_to_json(kilo),
        }
        print(json.dumps(obj, separators=(",", ":")))
        return
    rows = codex_rows(cfg, codex)
    if claude:
        source = claude.get("source", "")
        five_used = (claude.get("five_hour") or {}).get("used")
        week_used = (claude.get("week") or {}).get("used")
        common.log_usage_sample("Claude", "5h", common.remaining_from_used(five_used))
        common.log_usage_sample("Claude", "weekly", common.remaining_from_used(week_used))
        rows.extend(
            [
                row_from_used(cfg, "Claude", "5h", five_used, (claude.get("five_hour") or {}).get("resets_at"), source),
                row_from_used(cfg, "Claude", "weekly", week_used, (claude.get("week") or {}).get("resets_at"), source),
            ]
        )
    else:
        rows.extend(unavailable_rows("Claude"))
    rows.extend(copilot_rows(cfg, copilot))
    rows.extend(kilo_rows(cfg, _kilo_to_json(kilo)))
    if not cfg.no_header:
        print_dashboard_header(cfg)
        print_table_header(cfg)
    print_usage_rows(cfg, rows)


def _kilo_to_json(snap: Any) -> dict[str, Any]:
    """Project a Kilo ProviderSnapshot into a JSON-friendly dict."""
    scopes: list[dict[str, Any]] = []
    for scope in getattr(snap, "scopes", []) or []:
        scopes.append(
            {
                "name": scope.name,
                "kind": scope.kind,
                "ready": scope.ready,
                "reason": scope.reason,
                "remaining_percent": scope.remaining_percent,
                "remaining_amount": scope.remaining_amount,
                "total_amount": scope.total_amount,
                "currency": scope.currency,
                "reset_epoch": scope.reset_epoch,
                "resets_at": scope.resets_at,
                "label": scope.label,
                "source": scope.source,
            }
        )
    return {
        "provider": snap.provider,
        "available": snap.available,
        "reason": snap.reason,
        "source": snap.source,
        "selected_model": snap.selected_model,
        "scopes": scopes,
    }


def statusline_mode() -> None:
    text = sys.stdin.read()
    obj = common.read_json_text(text)
    if isinstance(obj, dict) and (obj.get("rate_limits") is not None or obj.get("rateLimits") is not None):
        cache = common.usage_cache_dir() / "claude-status.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    five = None
    week = None
    if isinstance(obj, dict):
        five = common.get_path(obj, (("rate_limits", "five_hour", "used_percentage"), ("rateLimits", "fiveHour", "usedPercent")))
        week = common.get_path(obj, (("rate_limits", "seven_day", "used_percentage"), ("rateLimits", "sevenDay", "usedPercent")))
    out = "Claude"
    five_rem = common.remaining_from_used(five)
    week_rem = common.remaining_from_used(week)
    if five_rem is not None:
        out += f" 5h {common.fmt_pct(five_rem)}% left"
    if week_rem is not None:
        out += f" weekly {common.fmt_pct(week_rem)}% left"
    print(out)


def log_once(cfg: Config) -> None:
    from io import StringIO
    import contextlib

    with contextlib.redirect_stdout(StringIO()):
        render_once(cfg)
    common.prune_usage_log()


def main(argv: list[str] | None = None) -> int:
    common.migrate_legacy_cache_dirs()
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    common.usage_cache_dir().mkdir(parents=True, exist_ok=True)
    if cfg.statusline_mode:
        statusline_mode()
        return 0
    if cfg.log_only:
        cfg.json_output = False
        cfg.show_remaining_time = False
        if cfg.watch_interval != "0":
            while True:
                log_once(cfg)
                time.sleep(float(cfg.watch_interval))
        log_once(cfg)
        return 0
    if cfg.watch_interval != "0":
        while True:
            from io import StringIO
            import contextlib

            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                render_once(cfg)
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            print(buf.getvalue(), end="")
            time.sleep(float(cfg.watch_interval))
    else:
        render_once(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
