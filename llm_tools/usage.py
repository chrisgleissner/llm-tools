from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import common
from .capacity import ProviderSnapshot


APP_NAME = "llm-usage"
PROVIDER_COL_WIDTH = 8
MODEL_COL_WIDTH = 7
TABLE_GAP_WIDTH = 3
SOURCE_COL_WIDTH = 18
PROGRESS_BAR_WIDTH = 10
# Right-aligned "100%" + space + 10-char bar.
REMAINING_COL_WIDTH = PROGRESS_BAR_WIDTH + 1 + 4
GUIDANCE_COL_WIDTH = 19
RESET_COL_WIDTH = 10
GUIDANCE_TOLERANCE_PP = 5.0

# Internal reason codes that mean "we could not measure usage right now". They
# are useful for the scheduler/capacity gating (see capacity.is_undetermined_reason)
# but read as jargon and overflow the Remaining column in the table, so the
# display layer collapses them to a single short word. Meaningful states such as
# balances, byok/local/ungated, or descriptive blocking reasons (rate-limited,
# budget-exhausted, insufficient-balance) are intentionally left untouched.
UNAVAILABLE_DISPLAY_REASONS = frozenset(
    {
        "inconclusive-usage",
        "missing-cli",
        "not-authenticated",
        "refresh-pending",
        "reader-error",
        "capture-error",
        "format-changed",
        "trust-prompt",
        "timeout",
        "no-local-data",
        "no local data",
        "unknown",
    }
)

# Opaque subscription rows display their fixed-subscription cost in
# the Remaining column instead of a percent / progress bar. The label
# is "prepaid $20/mo" for the most common prepaid shape; more elaborate
# cost shapes (e.g. multiple periods / currencies) are accepted but
# fall back to a generic "prepaid" prefix.
_FIXED_SUBSCRIPTION_PERIOD_LABELS = {
    "monthly": "mo",
    "yearly": "yr",
    "annual": "yr",
    "weekly": "wk",
    "daily": "day",
}

# ISO 4217 -> display symbol for the common prepaid currencies. Unknown
# codes fall through and render as the code itself so an unsupported
# currency never silently disappears.
_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "CAD": "C$",
    "AUD": "A$",
    "NZD": "NZ$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "KRW": "₩",
    "INR": "₹",
    "BRL": "R$",
    "MXN": "MX$",
    "CHF": "CHF",
    "SEK": "kr",
    "NOK": "kr",
    "DKK": "kr",
    "PLN": "zł",
    "ZAR": "R",
    "SGD": "S$",
    "HKD": "HK$",
}


def format_fixed_subscription(cost: dict[str, Any] | None) -> str:
    """Render the Remaining-cell text for a ``fixed_subscription`` route.

    Examples: ``prepaid $20/mo``, ``prepaid €15/mo``, ``prepaid JPY1500/mo``.
    Unknown ISO codes pass through unchanged (e.g. ``prepaid XYZ20/mo``) so
    a currency the renderer does not recognise never silently disappears.
    """
    if not isinstance(cost, dict):
        return "prepaid"
    amount = cost.get("amount")
    currency = cost.get("currency")
    period = str(cost.get("period") or "").strip().lower()
    suffix = _FIXED_SUBSCRIPTION_PERIOD_LABELS.get(period, period or "mo")

    def _fmt_amount(value: Any) -> str:
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            # Show integers without a trailing .0 so "$20/mo" reads
            # naturally; keep precision for genuinely fractional values.
            if value.is_integer():
                return str(int(value))
            return f"{value:g}"
        return str(value)

    text = _fmt_amount(amount)
    if amount is None:
        body = "prepaid"
    elif currency:
        # Render the common ISO currencies as their symbols so the
        # canonical "$20/mo" / "€15/mo" read naturally. Unknown codes
        # (e.g. an internal credit unit) fall through to the raw code.
        symbol = _CURRENCY_SYMBOLS.get(str(currency).upper())
        if symbol and symbol != str(currency).upper():
            body = f"prepaid {symbol}{text}"
        else:
            body = f"prepaid {currency}{text}"
    else:
        body = f"prepaid {text}"
    if not period:
        return body
    return f"{body}/{suffix}"


def format_opaque_remaining(scope: dict[str, Any] | None) -> str:
    """Render the Remaining-cell text for an opaque route.

    * Fixed-subscription cost -> ``prepaid $20/mo``.
    * Anything else            -> ``not metered``.

    The opaque row never displays a percentage, balance, or progress
    bar; the rest of the renderer must consult ``row.kind == "opaque"``
    and skip those branches. The cost object is read from the scope's
    ``extras`` so a single opaque row carries everything the renderer
    needs without a second lookup.
    """
    if not isinstance(scope, dict):
        return "not metered"
    extras = scope.get("extras") if isinstance(scope.get("extras"), dict) else {}
    cost_policy = extras.get("cost_policy")
    if cost_policy == "fixed_subscription":
        return format_fixed_subscription(
            {
                "amount": extras.get("cost_amount"),
                "currency": extras.get("cost_currency"),
                "period": extras.get("cost_period"),
            }
        )
    return "not metered"


def display_remaining(value: str) -> str:
    """Map an internal "couldn't measure" reason code to ``unavailable``.

    Percentages, balances, and unmetered states pass through unchanged.
    """
    if value in UNAVAILABLE_DISPLAY_REASONS:
        return "unavailable"
    return value


USAGE = """Usage: llm-usage
  llm-usage [options]

Shows remaining capacity per scope for:
  - Codex 5-hour window
  - Codex weekly / 7-day window
  - Codex Spark 5-hour and weekly windows
  - Claude Code 5-hour and weekly windows
  - Copilot monthly usage
  - Copilot AI credits (optional, with --show-copilot-credits)
  - Kilo balance, monthly budget, and BYOK/local/ungated state
  - MiniMax 5-hour and weekly windows (when the mmx CLI is on PATH)

Options:
  -j, --json                               Emit JSON instead of a table.
  -w, --watch SECONDS                      Refresh repeatedly.
  -C, --show-copilot-credits               Show Copilot AI credits row.
  -S, --show-source                        Show Source column.
  -s, --hide-source                        Hide Source column (default).
  -R, --show-remaining-time                Show Remaining Time column.
  -r, --hide-remaining-time                Hide Remaining Time column (default).
  -D, --show-daily-budget                  Show Guidance column (default).
  -d, --hide-daily-budget                  Hide Guidance column.
  -K, --show-codex-spark                   Show Codex Spark rows (default).
  -k, --hide-codex-spark                   Hide Codex Spark rows.
  -M, --copilot-monthly-reset-offset-days DAYS
                                           Day offset from month start for Copilot monthly reset.
  -t, --statusline                         Read Claude statusline JSON from stdin and cache it.
  -l, --log-only                           Sample providers and append to the usage log only.
  -n, --no-header                          Omit table header.
  -p, --provider-parallelism N             Provider readers to run concurrently (default: CPU cores).
  -h, --help                               Show this help.
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
        self.provider_parallelism = provider_parallelism(env)
        self.symbols_enabled = env.get("LLM_TOOLS_NO_SYMBOLS", "0") != "1"
        self.color_enabled = sys.stdout.isatty() and not env.get("LLM_USAGE_NO_COLOR") and env.get("TERM") != "dumb"
        # The progress indicator is purely stderr-side feedback while readers
        # query their (sometimes slow) providers. It is gated on stderr being a
        # TTY so it never leaks into pipes, batch scripts, or non-interactive
        # sessions (telnet without a PTY, cron, CI) — there it stays silent.
        self.progress_enabled = (
            sys.stderr.isatty()
            and env.get("TERM") != "dumb"
            and env.get("LLM_USAGE_NO_PROGRESS", "0") != "1"
        )
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
    # Per-model label (e.g. "Sonnet", "Spark"). Empty for a provider's
    # aggregate rows; populated for model-specific sub-rows so the table can
    # render a dedicated Model column under the provider section.
    model: str = ""
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
        if arg in ("-j", "--json"):
            cfg.json_output = True
            i += 1
        elif arg in ("-w", "--watch"):
            if i + 1 >= len(argv):
                common.err(f"{arg} requires seconds")
                raise SystemExit(2)
            cfg.watch_interval = argv[i + 1]
            i += 2
        elif arg in ("-C", "--show-copilot-credits"):
            cfg.show_copilot_credits = True
            i += 1
        elif arg in ("-S", "--show-source"):
            cfg.show_source = True
            i += 1
        elif arg in ("-s", "--hide-source"):
            cfg.show_source = False
            i += 1
        elif arg in ("-R", "--show-remaining-time"):
            cfg.show_remaining_time = True
            i += 1
        elif arg in ("-r", "--hide-remaining-time"):
            cfg.show_remaining_time = False
            i += 1
        elif arg in ("-D", "--show-daily-budget"):
            cfg.show_daily_budget = True
            i += 1
        elif arg in ("-d", "--hide-daily-budget"):
            cfg.show_daily_budget = False
            i += 1
        elif arg in ("-K", "--show-codex-spark"):
            cfg.show_codex_spark = True
            i += 1
        elif arg in ("-k", "--hide-codex-spark"):
            cfg.show_codex_spark = False
            i += 1
        elif arg in ("-M", "--copilot-monthly-reset-offset-days"):
            if i + 1 >= len(argv):
                common.err(f"{arg} requires DAYS")
                raise SystemExit(2)
            os.environ["LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS"] = argv[i + 1]
            i += 2
        elif arg in ("-t", "--statusline"):
            cfg.statusline_mode = True
            i += 1
        elif arg in ("-l", "--log-only"):
            cfg.log_only = True
            i += 1
        elif arg in ("-n", "--no-header"):
            cfg.no_header = True
            i += 1
        elif arg in ("-p", "--provider-parallelism"):
            if i + 1 >= len(argv):
                common.err(f"{arg} requires N")
                raise SystemExit(2)
            if not common.is_integer(argv[i + 1]) or int(argv[i + 1]) < 1:
                common.err(f"{arg} must be a positive integer")
                raise SystemExit(2)
            cfg.provider_parallelism = int(argv[i + 1])
            i += 2
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


def provider_parallelism(env: dict[str, str] | None = None) -> int:
    env = env or os.environ
    default = max(1, os.cpu_count() or 1)
    raw = env.get("LLM_USAGE_PROVIDER_PARALLELISM", "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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
    "unknown") are passed through unchanged. Internal "couldn't measure"
    reason codes are collapsed to ``unavailable`` so the column stays short.
    """
    value = display_remaining(value)
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


def provider_ready(rows: list[UsageRow], provider: str) -> bool:
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
    if rem is None:
        # No measurement at all (e.g. an unavailable provider) is not the same
        # as an exhausted window; "× empty" would wrongly imply quota was spent.
        return GuidanceInfo("· no rate data", "unknown")
    if rem <= 0:
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


def cell_clipped(width: int, text: str, gap: bool = False) -> str:
    """Render a cell that NEVER overflows its declared width.

    Overflowing text is clipped with a trailing "…" so the next column
    starts on the expected boundary. Used after dynamic width sizing
    so a long Provider (e.g. ``route:kilo-minimax-m3``) can no longer
    bleed into the Model column when the table is forced into a
    narrow terminal.
    """
    text = text or ""
    vlen = visible_len(text)
    if vlen <= width:
        return text + (" " * (width - vlen)) + (" " * TABLE_GAP_WIDTH if gap else "")
    if width <= 1:
        return "…" * width + (" " * TABLE_GAP_WIDTH if gap else "")
    # Keep the visible head of the text and replace the tail with "…".
    chars: list[str] = []
    used = 0
    for ch in text:
        w = 2 if unicodedata_width_wide(ch) else 1
        if used + w > width - 1:
            break
        chars.append(ch)
        used += w
    chars.append("…")
    return "".join(chars) + (" " * (width - visible_len("".join(chars)))) + (" " * TABLE_GAP_WIDTH if gap else "")


def unicodedata_width_wide(ch: str) -> bool:
    import unicodedata
    return unicodedata.east_asian_width(ch) in {"F", "W"}


def fit_columns(
    base_cols: list[tuple[str, int]],
    rows_text: list[dict[str, str]],
    terminal_width: int,
    has_source: bool,
) -> list[tuple[str, int]]:
    """Widen each column to fit the longest cell (or its label), then
    scale the non-essential columns down when the table would overflow
    the terminal. Order: widen first (no clipping), then trim the
    columns whose content is least load-bearing until the table fits.

    The trim floor for each column is the *longest cell seen so far*,
    not the label. That keeps things like ``weekly`` or
    ``subscription`` readable as long as the data is present, and
    only clips the trailing chars from longer cells (e.g. a long
    Guidance sentence) when the table is forced into a narrow
    terminal.
    """
    if not base_cols:
        return base_cols
    labels = {label: label for label, _ in base_cols}
    widths = {label: max(width, visible_len(label)) for label, width in base_cols}
    # Track the natural cell width so trim floors never drop below it.
    # Start at 0 so the floor reflects actual cell content, not the initial
    # column width, allowing trimming when the base width exceeds cell content.
    cell_widths: dict[str, int] = {label: 0 for label in widths}
    for row in rows_text:
        for label, current in cell_widths.items():
            value = row.get(label, "")
            natural = visible_len(value)
            cell_widths[label] = max(current, natural)
            if natural > widths[label]:
                widths[label] = natural
    if terminal_width > 0:
        n = len(base_cols) + (1 if has_source else 0)
        reserved = (n - 1) * TABLE_GAP_WIDTH
        total = sum(widths.values()) + reserved
        if total > terminal_width:
            overflow = total - terminal_width
            # Trim columns in priority order: Remaining Time first, then
            # Guidance, Resets in, Model, Scope. Never trim Provider /
            # Ready / Remaining below their natural content width. Scope
            # is the last to be trimmed because real cells are short
            # ("5h" / "weekly" / "subscription") and we do not want a
            # well-known window name to be clipped.
            trim_order = ["Remaining Time", "Guidance", "Resets in", "Model", "Scope"]
            for label in trim_order:
                if overflow <= 0:
                    break
                if label not in widths:
                    continue
                floor = max(visible_len(labels.get(label, label)), cell_widths.get(label, 0))
                can_give = widths[label] - floor
                if can_give <= 0:
                    continue
                give = min(can_give, overflow)
                widths[label] -= give
                overflow -= give
    return [(label, widths[label]) for label, _ in base_cols]


def rule(width: int, gap: bool = False, char: str = "-") -> str:
    return char * width + (" " * TABLE_GAP_WIDTH if gap else "")


def table_columns(cfg: Config, show_model: bool = False) -> list[tuple[str, int]]:
    cols = [("Provider", PROVIDER_COL_WIDTH)]
    if show_model:
        cols.append(("Model", MODEL_COL_WIDTH))
    cols += [("Ready", 5), ("Scope", 7), ("Remaining", REMAINING_COL_WIDTH)]
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


def print_table_header(cfg: Config, show_model: bool = False) -> None:
    cols = table_columns(cfg, show_model)
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


def table_fixed_width(cfg: Config, show_model: bool = False) -> int:
    cols = table_columns(cfg, show_model)
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


def row_from_used(cfg: Config, provider: str, window: str, used: Any, reset: Any, source: str, display_provider: str | None = None, model: str = "") -> UsageRow:
    remaining = common.remaining_from_used(used)
    remaining_time = common.estimate_remaining_time_from_log(provider, window, remaining) if cfg.show_remaining_time else "-"
    return UsageRow(display_provider or provider, window, remaining, row_left_text(remaining), reset, source, remaining_time, model=model)


def unavailable_rows(provider: str) -> list[UsageRow]:
    return [
        UsageRow(provider, "5h", None, "-", None, "no local data"),
        UsageRow(provider, "weekly", None, "-", None, "no local data"),
    ]


def provider_unavailable_rows(provider: str, source: str, reason: str) -> list[UsageRow]:
    return [
        UsageRow(provider, "5h", None, reason or "-", None, source or "no local data"),
        UsageRow(provider, "weekly", None, reason or "-", None, source or "no local data"),
    ]


def print_value_row(cfg: Config, provider: str, window: str, remaining: str, remaining_time: str, reset_text: str, time_to_reset: str, source: str, daily_value: float | None = None) -> None:
    rem = common.num(remaining.rstrip("%")) if isinstance(remaining, str) and remaining.endswith("%") else None
    reset = None if time_to_reset == "-" else reset_text
    row = UsageRow(provider=provider, scope=window, remaining=rem, left_text=remaining, reset=reset, source=source, remaining_time=remaining_time or "-")
    print_usage_rows(cfg, [row])


def row_values(cfg: Config, row: UsageRow, display_provider: str, ready_text: str, display_model: str = "") -> dict[str, str]:
    # Opaque rows carry their own short label ("✓ usable" / "! retry
    # in Xm") so they do not flow through the standard guidance
    # pipeline (which would emit "no rate data" for a row with no
    # remaining percent / reset). The "ready" state is encoded on
    # the row: ``remaining = 1.0`` means ready, ``None`` means
    # blocked, with the retry hint derived from ``reset``.
    if row.kind == "opaque":
        if row.remaining is not None:
            guidance = "✓ usable"
        else:
            wait_until = row.reset if isinstance(row.reset, int) else None
            if isinstance(wait_until, int):
                minutes = max(1, (wait_until - common.now_epoch()) // 60)
                guidance = f"! retry in {int(minutes)}m"
            else:
                guidance = "! blocked"
    else:
        guidance = render_guidance(row.provider, row.scope, row.remaining, row.reset, cfg)
    values = {
        "Provider": display_provider,
        "Model": display_model,
        "Ready": ready_text,
        "Scope": row.scope,
        "Remaining": render_remaining(row.left_text, cfg),
        "Guidance": guidance,
        "Remaining Time": row.remaining_time,
        "Resets in": format_reset(row.reset, cfg),
    }
    return values


def print_usage_rows(cfg: Config, rows: list[UsageRow]) -> None:
    show_model = any(row.model for row in rows)
    base_cols = table_columns(cfg, show_model)
    # First pass: build the display values so the column widths can be
    # computed against the actual cell text. Doing this in a separate
    # pass keeps a long Provider (e.g. ``route:kilo-minimax-m3``) from
    # bleeding into the Model column when the terminal is narrow.
    previous_provider = ""
    previous_model = ""
    rendered: list[tuple[UsageRow, dict[str, str], bool]] = []
    rows_text: list[dict[str, str]] = []
    for row in rows:
        first_of_provider = row.provider != previous_provider
        if first_of_provider:
            previous_model = ""
        display_provider = row.provider if first_of_provider else ""
        ready_text = render_ready(1 if provider_ready(rows, row.provider) else 0, cfg) if display_provider else ""
        # Show the model label only on the first row of each model sub-block so
        # the column stays uncluttered when a model spans several scope rows.
        display_model = row.model if (row.model and row.model != previous_model) else ""
        previous_provider = row.provider
        previous_model = row.model
        values = row_values(cfg, row, display_provider, ready_text, display_model)
        rendered.append((row, values, first_of_provider))
        rows_text.append(values)
    cols = fit_columns(base_cols, rows_text, cfg.terminal_width, has_source=cfg.show_source)
    last = len(cols) - 1
    emitted_blank = True
    for row, values, first_of_provider in rendered:
        if first_of_provider and not emitted_blank:
            print()
        emitted_blank = False
        parts = []
        for idx, (label, width) in enumerate(cols):
            gap = idx != last or cfg.show_source
            parts.append(cell_clipped(width, values.get(label, ""), gap))
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


def claude_rows(cfg: Config, claude_snap: Any) -> list[UsageRow]:
    """Render Claude into aggregate 5h/weekly rows plus any per-model weekly rows.

    Anthropic exposes per-model weekly limits (e.g. Sonnet-only) in addition to
    the aggregate window. Those are surfaced as extra rows under the same Claude
    section with the model named in the Model column. They are display-only and
    do not affect scheduler gating (see :class:`ProviderSnapshot.model_scopes`).
    """
    if not getattr(claude_snap, "available", False):
        return provider_unavailable_rows(
            "Claude",
            getattr(claude_snap, "source", ""),
            getattr(claude_snap, "reason", "") or "no-local-data",
        )
    legacy = _legacy_claude(claude_snap) or {}
    source = legacy.get("source", "")
    five_used = (legacy.get("five_hour") or {}).get("used")
    week_used = (legacy.get("week") or {}).get("used")
    common.log_usage_sample("Claude", "5h", common.remaining_from_used(five_used))
    common.log_usage_sample("Claude", "weekly", common.remaining_from_used(week_used))
    rows = [
        row_from_used(cfg, "Claude", "5h", five_used, (legacy.get("five_hour") or {}).get("resets_at"), source),
        row_from_used(cfg, "Claude", "weekly", week_used, (legacy.get("week") or {}).get("resets_at"), source),
    ]
    for scope in getattr(claude_snap, "model_scopes", None) or []:
        model = str((getattr(scope, "extras", None) or {}).get("model") or "")
        if not model:
            continue
        rem = scope.remaining_percent
        used = (100.0 - rem) if rem is not None else None
        common.log_usage_sample(f"Claude {model}", scope.name, rem)
        rows.append(row_from_used(cfg, f"Claude {model}", scope.name, used, scope.resets_at, scope.source or source, "Claude", model=model))
    return rows


def print_claude_rows(cfg: Config, claude_snap: Any) -> None:
    print_usage_rows(cfg, claude_rows(cfg, claude_snap))


def codex_rows(cfg: Config, codex_json: dict[str, Any] | None) -> list[UsageRow]:
    if not codex_json:
        return unavailable_rows("Codex")
    if codex_json.get("available") is False:
        return provider_unavailable_rows("Codex", codex_json.get("source", ""), codex_json.get("reason", "unavailable"))
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
        # All Codex models live under the same "Codex" provider section; the
        # specific model (e.g. Spark) is surfaced via the Model column instead
        # of overflowing the Provider column with a long combined name.
        model = "Spark" if is_spark else ""
        source = row.get("source") or codex_json.get("source", "")
        five_used = (row.get("five_hour") or {}).get("used")
        week_used = (row.get("week") or {}).get("used")
        common.log_usage_sample(provider, "5h", common.remaining_from_used(five_used))
        common.log_usage_sample(provider, "weekly", common.remaining_from_used(week_used))
        out.append(row_from_used(cfg, provider, "5h", five_used, (row.get("five_hour") or {}).get("resets_at"), source, "Codex", model=model))
        out.append(row_from_used(cfg, provider, "weekly", week_used, (row.get("week") or {}).get("resets_at"), source, "Codex", model=model))
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
            extras = scope.get("extras") or {}
            if extras.get("spent") and amount is not None:
                text = format_spent(amount, currency)
                # Spent-cost rows are informational; the provider is ready when
                # the snapshot says the CLI is present and functional.
                row_remaining: float | None = 1.0 if kilo_json.get("available") else None
            else:
                text = format_balance(amount, currency)
                row_remaining = amount
            rows.append(
                UsageRow(
                    "Kilo",
                    "balance",
                    row_remaining,
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


def format_spent(amount: float | None, currency: str | None) -> str:
    """Format a "spent" amount: ``spent $5.96``.

    Used by Kilo/OpenCode stats to surface the cost we observed when
    the user has not configured a balance, so the table does not
    degrade to ``inconclusive-usage`` once the parser can read the CLI
    output.
    """
    base = format_balance(amount, currency)
    if base == "-":
        return "-"
    return f"spent {base}"


def print_kilo_rows(cfg: Config, kilo_json: dict[str, Any] | None) -> None:
    print_usage_rows(cfg, kilo_rows(cfg, kilo_json))


def opencode_rows(cfg: Config, opencode_json: dict[str, Any] | None) -> list[UsageRow]:
    """Render OpenCode scopes into a flat list of table rows.

    OpenCode does not have session windows: its scopes are balance,
    budget, and (optionally) byok/local/ungated. Each scope becomes its
    own row with a ``scope`` name that the table renders in the Scope
    column.
    """
    from .capacity import CapacityKind

    if not opencode_json:
        return [UsageRow("OpenCode", "balance", None, "unavailable", None, "opencode cli")]
    source = opencode_json.get("source", "opencode cli")
    if opencode_json.get("available") is False:
        reason = opencode_json.get("reason") or "unavailable"
        rows: list[UsageRow] = []
        rows.append(UsageRow("OpenCode", "balance", None, reason, None, source))
        return rows
    scopes = opencode_json.get("scopes") if isinstance(opencode_json.get("scopes"), list) else []
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
                    "OpenCode",
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
            extras = scope.get("extras") or {}
            if extras.get("spent") and amount is not None:
                text = format_spent(amount, currency)
                # Spent-cost rows are informational; the provider is ready when
                # the snapshot says the CLI is present and functional.
                row_remaining: float | None = 1.0 if opencode_json.get("available") else None
            else:
                text = format_balance(amount, currency)
                row_remaining = amount
            rows.append(
                UsageRow(
                    "OpenCode",
                    "balance",
                    row_remaining,
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
            remaining_time = (
                common.estimate_remaining_time_from_log("OpenCode", "budget", rem)
                if cfg.show_remaining_time
                else "-"
            )
            rows.append(
                UsageRow(
                    "OpenCode",
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
        rows.append(UsageRow("OpenCode", "balance", None, "unavailable", None, source))
    return rows


def print_opencode_rows(cfg: Config, opencode_json: dict[str, Any] | None) -> None:
    print_usage_rows(cfg, opencode_rows(cfg, opencode_json))


MINIMAX_DISPLAY_NAME = "MiniMax"


def minimax_rows(cfg: Config, minimax_json: dict[str, Any] | None) -> list[UsageRow]:
    """Render MiniMax scopes into a flat list of table rows.

    MiniMax exposes the same 5h/weekly reset-window shape Claude Code
    and Codex use, sourced from ``mmx quota show --output json``. When
    the ``mmx`` binary is not installed and no env-var fallback is
    configured the reader reports ``available=false`` and we render a
    single ``unavailable`` row so the user can see why.
    """
    from .capacity import CapacityKind

    if not minimax_json:
        return [UsageRow(MINIMAX_DISPLAY_NAME, "5h", None, "unavailable", None, "mmx cli")]
    source = minimax_json.get("source", "mmx cli")
    if minimax_json.get("available") is False:
        reason = minimax_json.get("reason") or "unavailable"
        return [UsageRow(MINIMAX_DISPLAY_NAME, "5h", None, reason, None, source)]
    scopes = minimax_json.get("scopes") if isinstance(minimax_json.get("scopes"), list) else []
    rows: list[UsageRow] = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        if scope.get("kind") != CapacityKind.RESET_WINDOW:
            continue
        name = str(scope.get("name", "5h"))
        rem = scope.get("remaining_percent")
        reset = scope.get("reset_epoch")
        if rem is None:
            text = "unavailable"
        else:
            text = row_left_text(rem)
        common.log_usage_sample(MINIMAX_DISPLAY_NAME, name, rem if isinstance(rem, (int, float)) else None)
        remaining_time = (
            common.estimate_remaining_time_from_log(MINIMAX_DISPLAY_NAME, name, rem)
            if cfg.show_remaining_time
            else "-"
        )
        rows.append(
            UsageRow(
                MINIMAX_DISPLAY_NAME,
                name,
                rem if isinstance(rem, (int, float)) else None,
                text,
                reset,
                source,
                remaining_time or "-",
                kind=CapacityKind.RESET_WINDOW,
            )
        )
    if not rows:
        return [UsageRow(MINIMAX_DISPLAY_NAME, "5h", None, "unavailable", None, source)]
    return rows


def print_minimax_rows(cfg: Config, minimax_json: dict[str, Any] | None) -> None:
    print_usage_rows(cfg, minimax_rows(cfg, minimax_json))


def unavailable_snapshot(provider: str, source: str, reason: str = "reader-error") -> ProviderSnapshot:
    return ProviderSnapshot(provider=provider, available=False, reason=reason, source=source)


class ProgressReporter:
    """Ephemeral, single-line progress feedback for slow provider reads.

    Renders an animated spinner plus a completed/total counter to ``stderr``
    and erases itself entirely once the data is ready, leaving the terminal
    exactly as it was. It is a no-op when ``enabled`` is false (non-TTY stderr),
    so JSON output, pipes, and batch scripts stay byte-clean.
    """

    FRAMES_UNICODE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    FRAMES_ASCII = ("|", "/", "-", "\\")

    def __init__(
        self,
        enabled: bool,
        symbols: bool = True,
        stream: Any = None,
        label: str = "refreshing usage",
        interval: float = 0.1,
        anchor: tuple[int, int] | None = None,
    ) -> None:
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr
        self.label = label
        self.interval = interval
        # When ``anchor`` (a 1-based ``(row, col)``) is set, the spinner draws
        # itself at that fixed screen cell instead of on the current line. The
        # cursor is saved/restored around every write (DEC ESC 7 / ESC 8, the
        # most widely supported sequence — vt100, xterm, the Linux console,
        # tmux, and screen all honour it) so the caller can keep printing the
        # body below without the spinner thread stealing the cursor. This is
        # what lets the watch dashboard show "refreshing" to the right of the
        # clock instead of trailing the table.
        self.anchor = anchor
        self.frames = self.FRAMES_UNICODE if symbols else self.FRAMES_ASCII
        self._total = 0
        self._done = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def begin(self, total: int) -> None:
        with self._lock:
            self._total = total

    def advance(self, step: int = 1) -> None:
        with self._lock:
            self._done += step

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._render(self.frames[0])
        index = 0
        while not self._stop.wait(self.interval):
            index += 1
            self._render(self.frames[index % len(self.frames)])

    def _render(self, frame: str) -> None:
        with self._lock:
            done, total = self._done, self._total
        count = f" {done}/{total}" if total else ""
        try:
            if self.anchor is not None:
                row, col = self.anchor
                self.stream.write(f"\0337\033[{row};{col}H\033[K{frame} {self.label}{count}\0338")
            else:
                self.stream.write(f"\r\033[K{frame} {self.label}{count}")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1)
        self._thread = None
        try:
            if self.anchor is not None:
                row, col = self.anchor
                self.stream.write(f"\0337\033[{row};{col}H\033[K\0338")
            else:
                self.stream.write("\r\033[K")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def __enter__(self) -> "ProgressReporter":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def read_provider(name: str, reader: Any, fallback: Any) -> Any:
    try:
        return reader()
    except Exception:
        return fallback() if callable(fallback) else fallback


def read_all_provider_data(cfg: Config, progress: "ProgressReporter | None" = None) -> dict[str, Any]:
    from .providers import (
        read_claude_snapshot,
        read_copilot_snapshot,
        read_kilo,
        read_minimax,
        read_opencode,
    )

    readers: dict[str, tuple[Any, Any]] = {
        "codex": (
            common.read_codex,
            lambda: {"provider": "codex", "available": False, "reason": "reader-error", "source": "~/.codex/sessions"},
        ),
        "claude": (
            read_claude_snapshot,
            lambda: unavailable_snapshot("claude", "claude reader"),
        ),
        "copilot": (
            read_copilot_snapshot,
            lambda: unavailable_snapshot("copilot", "copilot cli"),
        ),
        "kilo": (
            read_kilo,
            lambda: unavailable_snapshot("kilo", "kilo cli"),
        ),
        "opencode": (
            read_opencode,
            lambda: unavailable_snapshot("opencode", "opencode cli"),
        ),
        "minimax": (
            read_minimax,
            lambda: unavailable_snapshot("minimax", "mmx cli"),
        ),
    }
    if progress is not None:
        progress.begin(len(readers))
    if cfg.provider_parallelism <= 1:
        out = {}
        for name, (reader, fallback) in readers.items():
            out[name] = read_provider(name, reader, fallback)
            if progress is not None:
                progress.advance()
        return out
    out = {}
    with ThreadPoolExecutor(max_workers=cfg.provider_parallelism) as pool:
        futures = {
            pool.submit(read_provider, name, reader, fallback): name
            for name, (reader, fallback) in readers.items()
        }
        for future in as_completed(futures):
            out[futures[future]] = future.result()
            if progress is not None:
                progress.advance()
    return out


def _fetch_provider_data(cfg: Config, anchor: tuple[int, int] | None = None) -> dict[str, Any]:
    """Read every provider, animating the spinner while the slow reads run.

    ``anchor`` pins the spinner to a fixed screen cell (used by the watch
    dashboard to park "refreshing" beside the clock); the default ``None`` keeps
    the classic single-line, self-erasing behaviour.
    """
    progress = ProgressReporter(
        enabled=cfg.progress_enabled and not cfg.log_only,
        symbols=cfg.symbols_enabled,
        anchor=anchor,
    )
    progress.start()
    try:
        return read_all_provider_data(cfg, progress=progress)
    finally:
        progress.stop()


def _emit_json(cfg: Config, provider_data: dict[str, Any]) -> None:
    claude_snap = provider_data["claude"]
    claude_json = (
        common.json_for_provider(_legacy_claude(claude_snap), "claude")
        if claude_snap.available
        else {
            "provider": "claude",
            "available": False,
            "reason": claude_snap.reason,
            "source": claude_snap.source,
        }
    )
    obj = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "codex": common.json_for_provider(provider_data["codex"], "codex"),
        "claude": claude_json,
        "copilot": _legacy_copilot(provider_data["copilot"], cfg.show_copilot_credits),
        "kilo": _kilo_to_json(provider_data["kilo"]),
        "opencode": _opencode_to_json(provider_data["opencode"]),
        "minimax": _minimax_to_json(provider_data["minimax"]),
    }
    # Route mode is opt-in: the ``routes`` key only appears when at
    # least one route is configured. Existing JSON consumers keep
    # working unchanged when the route table is empty. A misconfigured
    # route table is fatal: surface the config error to the user
    # rather than silently dropping the routes section.
    try:
        routes = route_decision_summary()
    except SystemExit:
        raise
    except Exception as exc:
        common.err(f"routes: failed to render configured routes: {exc}")
        routes = []
    if routes:
        obj["routes"] = routes
    print(json.dumps(obj, separators=(",", ":")))


def _build_usage_rows(cfg: Config, provider_data: dict[str, Any]) -> tuple[list[Any], bool]:
    rows = claude_rows(cfg, provider_data["claude"])
    rows.extend(codex_rows(cfg, provider_data["codex"]))
    rows.extend(copilot_rows(cfg, _legacy_copilot(provider_data["copilot"], False)))
    rows.extend(kilo_rows(cfg, _kilo_to_json(provider_data["kilo"])))
    rows.extend(minimax_rows(cfg, _minimax_to_json(provider_data["minimax"])))
    rows.extend(opencode_rows(cfg, _opencode_to_json(provider_data["opencode"])))
    # Route rows are appended in their declared config order so the
    # caller controls grouping. They sit beneath the per-provider
    # aggregate rows; an empty / unconfigured route table is a
    # no-op. A misconfigured route table is fatal: the config
    # loader surfaces the parse error at startup and we want the
    # same hard fail in the table renderer.
    try:
        rows.extend(route_rows(cfg))
    except FileNotFoundError:
        pass
    show_model = any(row.model for row in rows)
    return rows, show_model


def route_rows(cfg: Config) -> list[UsageRow]:
    """Render configured routes (from ``[routes.<id>]``) as table rows.

    Returns an empty list when no routes are configured or when the
    config file is missing. Each row uses the route id as the
    ``Provider`` column value so users can distinguish multiple
    routes that share a launch provider (e.g. two Kilo routes with
    different models).
    """
    from . import config as toolconfig
    from .routes import usage_snapshot_and_decision_for_route

    try:
        conf = toolconfig.load_config()
    except (OSError, FileNotFoundError):
        return []
    routes = toolconfig.parse_routes(conf)
    if not routes:
        return []
    out: list[UsageRow] = []
    for route_id, route in routes.items():
        try:
            snapshot, decision = usage_snapshot_and_decision_for_route(
                route, "auto", "1", "60"
            )
        except Exception:
            continue
        scope = (snapshot.get("scopes") or [{}])[0] if isinstance(snapshot, dict) else {}
        ready = bool(decision.get("usable"))
        cost_obj = snapshot.get("cost") or {}
        cost_policy = cost_obj.get("policy") if isinstance(cost_obj, dict) else None
        kind = str(scope.get("kind") or "")
        if kind == "opaque" and cost_policy == "fixed_subscription":
            left_text = format_fixed_subscription(cost_obj if isinstance(cost_obj, dict) else None)
        elif kind == "opaque":
            left_text = "not metered"
        elif ready:
            rem = scope.get("remaining_percent")
            if isinstance(rem, (int, float)):
                left_text = row_left_text(float(rem))
            else:
                left_text = "usable"
        else:
            left_text = display_remaining(str(decision.get("reason") or "blocked"))
        # Guidance text. Opaque rows show ✓ usable; blocked opaque rows
        # show the retry-after hint; everything else falls back to the
        # standard guidance.
        if kind == "opaque" and ready:
            guidance_text = "✓ usable"
        elif kind == "opaque" and not ready:
            wait_until = decision.get("wait_until")
            if isinstance(wait_until, int):
                minutes = max(1, (wait_until - common.now_epoch()) // 60)
                guidance_text = f"! retry in {int(minutes)}m"
            else:
                guidance_text = "! blocked"
        else:
            guidance_text = render_guidance(
                route.provider,
                str(scope.get("name") or ""),
                scope.get("remaining_percent"),
                scope.get("reset_epoch"),
                cfg,
            )
        extras = scope.get("extras") if isinstance(scope, dict) else None
        model_label = ""
        if isinstance(extras, dict) and extras.get("model"):
            model_label = str(extras.get("model"))
        elif route.model:
            model_label = route.model
        out.append(
            UsageRow(
                provider=f"route:{route_id}",
                scope=str(scope.get("name") or route.capacity.scope or "subscription"),
                remaining=1.0 if ready else None,
                left_text=left_text,
                reset=scope.get("reset_epoch") if isinstance(scope, dict) else None,
                source=str(snapshot.get("source") or "config:route"),
                remaining_time="-",
                model=model_label,
                amount=(cost_obj.get("amount") if isinstance(cost_obj, dict) else None),
                currency=(cost_obj.get("currency") if isinstance(cost_obj, dict) else None),
                kind=kind,
                label=str(scope.get("label") or "") if isinstance(scope, dict) else "",
            )
        )
    return out


def route_decision_summary() -> list[dict[str, Any]]:
    """Project the current route config into a JSON-friendly list.

    Each entry contains ``route``, ``provider``, ``selected_model``,
    ``available``, ``reason``, ``source``, ``scopes``, and ``cost``.
    Returns an empty list when no routes are configured.
    """
    from . import config as toolconfig
    from .routes import route_to_json, usage_snapshot_and_decision_for_route

    # A bad config (e.g. unknown capacity policy) must be fatal: the
    # config loader already calls ``_fail`` which raises SystemExit(2)
    # with a descriptive message. We deliberately re-raise it so the
    # bad config shows up in the user's terminal, not in a silently
    # dropped routes section.
    try:
        conf = toolconfig.load_config()
    except (OSError, FileNotFoundError):
        return []
    routes = toolconfig.parse_routes(conf)
    if not routes:
        return []
    out: list[dict[str, Any]] = []
    for route_id, route in routes.items():
        try:
            snapshot, _decision = usage_snapshot_and_decision_for_route(
                route, "auto", "1", "60"
            )
        except Exception:
            continue
        out.append(route_to_json(snapshot))
    return out


def _capture(fn: Any) -> list[str]:
    """Run a print-based renderer and return its output as lines (no trailing blank)."""
    from io import StringIO
    import contextlib

    buf = StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    text = buf.getvalue()
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def render_once(cfg: Config) -> None:
    provider_data = _fetch_provider_data(cfg)
    if cfg.json_output:
        _emit_json(cfg, provider_data)
        return
    rows, show_model = _build_usage_rows(cfg, provider_data)
    if not cfg.no_header:
        print_dashboard_header(cfg)
        print_table_header(cfg, show_model)
    print_usage_rows(cfg, rows)


def render_watch_frame(cfg: Config) -> None:
    """Render one watch frame, redrawing in place with the refresh spinner
    docked to the right of the clock.

    The header (which carries the timestamp) needs no provider data, so it is
    painted *first*; the spinner then animates beside the clock while the slow
    provider reads run, and the table fills in underneath once the data lands.
    Lines are cleared individually (``ESC[K``) and the frame is closed with
    ``ESC[J`` rather than a full ``ESC[2J`` wipe, so the dashboard updates
    without the flash you get from clearing the whole screen — and using only
    the most portable CSI sequences keeps it correct under tmux, screen, and a
    raw telnet PTY alike.
    """
    out = sys.stdout
    is_tty = out.isatty()
    # The inline-spinner choreography needs a TTY, the plain table layout, and a
    # header to anchor to. Anything else (piped output, JSON, --no-header) falls
    # back to the simple clear-and-redraw path.
    if not is_tty or cfg.json_output or cfg.no_header:
        provider_data = _fetch_provider_data(cfg)
        if is_tty:
            out.write("\033[2J\033[H")
        if cfg.json_output:
            _emit_json(cfg, provider_data)
        else:
            rows, show_model = _build_usage_rows(cfg, provider_data)
            if not cfg.no_header:
                print_dashboard_header(cfg)
                print_table_header(cfg, show_model)
            print_usage_rows(cfg, rows)
        return

    # 1. Home the cursor (no full-screen wipe → no flicker) and paint the
    #    data-independent header right away.
    out.write("\033[H")
    header_lines = _capture(lambda: print_dashboard_header(cfg))
    for line in header_lines:
        out.write(f"{line}\033[K\n")
    out.flush()

    # 2. Dock the spinner one column past the header's first line (the clock).
    spinner_col = len(header_lines[0]) + 2
    provider_data = _fetch_provider_data(cfg, anchor=(1, spinner_col))

    # 3. The data is in — fill in the table beneath the header, clearing each
    #    line, then erase any rows left over from a previous, taller frame.
    rows, show_model = _build_usage_rows(cfg, provider_data)
    body_lines = _capture(lambda: (print_table_header(cfg, show_model), print_usage_rows(cfg, rows)))
    for line in body_lines:
        out.write(f"{line}\033[K\n")
    out.write("\033[J")
    out.flush()


def _legacy_codex(snap: Any) -> dict[str, Any] | None:
    """Deprecated: Codex JSON output keeps the legacy wire format
    (``rows`` array with per-model entries) and is read directly via
    ``common.read_codex``. This helper is kept for the few call sites
    that still need the snapshot projection.
    """
    if not snap.available:
        return None
    out: dict[str, Any] = {
        "provider": snap.provider,
        "source": snap.source,
        "rows": [],
    }
    five = next((s for s in snap.scopes if s.name == "5h"), None)
    week = next((s for s in snap.scopes if s.name == "weekly"), None)
    out["five_hour"] = (
        {"resets_at": five.resets_at, "used": (100.0 - five.remaining_percent) if five.remaining_percent is not None else None}
        if five
        else None
    )
    out["week"] = (
        {"resets_at": week.resets_at, "used": (100.0 - week.remaining_percent) if week.remaining_percent is not None else None}
        if week
        else None
    )
    if snap.selected_model:
        out["plan"] = snap.selected_model
    return out


def _legacy_claude(snap: Any) -> dict[str, Any] | None:
    if not snap.available:
        return None
    out: dict[str, Any] = {"provider": snap.provider, "source": snap.source}
    for src_name, target in (("5h", "five_hour"), ("weekly", "week")):
        scope = next((s for s in snap.scopes if s.name == src_name), None)
        if scope is None:
            continue
        out[target] = {
            "resets_at": scope.resets_at,
            "used": (100.0 - scope.remaining_percent) if scope.remaining_percent is not None else None,
        }
    return out


def _legacy_copilot(snap: Any, show_credits: bool) -> dict[str, Any] | None:
    if not snap.available:
        return {
            "provider": snap.provider,
            "source": snap.source,
            "available": False,
            "reason": snap.reason or "unavailable",
        }
    monthly = next((s for s in snap.scopes if s.name == "monthly"), None)
    out: dict[str, Any] = {
        "provider": snap.provider,
        "source": snap.source,
        "available": True,
    }
    if monthly is not None and monthly.remaining_percent is not None:
        used = max(0.0, min(100.0, 100.0 - monthly.remaining_percent))
        out["monthly"] = {"used": used, "remaining": monthly.remaining_percent}
    return out


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
                "extras": dict(getattr(scope, "extras", {}) or {}),
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


def _opencode_to_json(snap: Any) -> dict[str, Any]:
    """Project an OpenCode ProviderSnapshot into a JSON-friendly dict.

    Mirrors :func:`_kilo_to_json`: the snapshot's :class:`CapacityScope`
    objects are flattened into plain dicts so the JSON output stays in
    sync with the generic capacity model.
    """
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
                "extras": dict(getattr(scope, "extras", {}) or {}),
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


def _minimax_to_json(snap: Any) -> dict[str, Any]:
    """Project a MiniMax ProviderSnapshot into a JSON-friendly dict.

    Same flattening strategy as :func:`_kilo_to_json`. The snapshot's
    :class:`CapacityScope` objects are translated to plain dicts so
    the JSON output stays in sync with the generic capacity model.
    """
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
                "extras": dict(getattr(scope, "extras", {}) or {}),
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
        if sys.stdout.isatty():
            print("\033[2J\033[H", end="")  # one clean wipe before the first redraw-in-place frame
        while True:
            render_watch_frame(cfg)
            time.sleep(float(cfg.watch_interval))
    else:
        render_once(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
