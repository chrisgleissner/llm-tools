"""The eight mission identity hues stay perceptually separated in every theme."""

from __future__ import annotations

import itertools

import pytest

from llm_tools import bridge_theme as theme

# Minimum pairwise OKLab distance (x100) between the eight mission hues. About 2 is the
# just-noticeable difference; the previous palettes had pairs at 5.9-6.8 (indigo, blue and
# slate read as one blue in the light theme). 10 keeps every pair clearly different at a glance.
MISSION_HUE_MIN_DISTANCE = 10.0


def _oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def linear(channel: int) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


@pytest.mark.parametrize(("name", "polarity"), [("dark", "dark"), ("light", "light"), ("high-contrast", "dark"), ("high-contrast", "light")])
def test_mission_hues_are_perceptually_separated(name: str, polarity: str) -> None:
    resolved = theme.resolve({"LLM_BRIDGE_THEME": name, "LLM_BRIDGE_BACKGROUND": polarity})
    labs = [_oklab(theme.xterm_rgb(resolved.style(f"mission_{n}").fg)) for n in range(1, 9)]
    closest = min(100 * sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5 for a, b in itertools.combinations(labs, 2))
    assert closest >= MISSION_HUE_MIN_DISTANCE, (name, polarity, closest)


def test_hue_names_follow_the_eight_slots() -> None:
    assert len(theme.MISSION_HUE_NAMES) == theme.MISSION_COUNT == 8
    assert len(set(theme.MISSION_HUE_NAMES)) == 8
