"""Mission lookup, ordering and liveness shared by the command line, the console and the switcher.

``resolve_mission_ref`` turns an ID, a unique ID prefix or the current folder into one mission ID.
``order_missions`` is the single order used by ``mission list``, ``missions`` and the ambiguity
errors. ``mission_live`` says whether a mission has a controller or a provider station. The
database reads go through named ``BridgeStore`` methods (``mission_id_if_exists``,
``missions_with_id_prefix``, ``missions_newest_first``); this module holds no SQL.

``bridge`` re-exports these names, so ``bridge.resolve_mission_ref`` keeps working.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bridge_states import FINISHED_MISSION_STATUSES, LIVE_MISSION_STATUSES
from .bridge_store import controller_running

if TYPE_CHECKING:
    from .bridge_store import BridgeStore


MIN_MISSION_PREFIX = 4


class MissionNotFound(ValueError):
    """No saved mission matches the given ID or prefix."""


def station_live(mission_id: str) -> bool:
    """Whether a Bridge-owned provider station is running for this mission."""
    from . import bridge_switcher

    return bool(bridge_switcher.default_station_probe(mission_id))


def mission_live(mission_id: str) -> bool:
    return controller_running(mission_id) or station_live(mission_id)


def order_missions(rows: list[Any]) -> list[Any]:
    """Missions with a controller or station first, then unfinished before finished, newest first.

    ``mission list``, ``missions`` and the ambiguity errors share this order.
    """
    newest = sorted(rows, key=lambda row: (str(row["updated_at"]), str(row["id"])), reverse=True)
    return sorted(newest, key=lambda row: (not mission_live(str(row["id"])), str(row["status"]) in FINISHED_MISSION_STATUSES))


def mission_choices(rows: list[Any]) -> str:
    return "\n".join(
        f"  {str(row['id'])[:8]}  {str(row['name'])[:40]}  {row['status']}" + ("  (live)" if mission_live(str(row["id"])) else "")
        for row in order_missions(rows)
    )


def resolve_mission_ref(
    store: BridgeStore,
    ref: str | None,
    *,
    cwd: str | Path | None = None,
    read_only: bool = False,
    prefer_station: bool = False,
) -> str:
    """Return the full mission ID for an ID, a unique ID prefix, or the current folder.

    With no ``ref`` the single unfinished mission whose folder equals or
    contains the current directory is used. A live mission (created, running,
    paused or stopping) wins over stopped or failed ones in the same folder.
    ``prefer_station`` narrows several matches to the ones with a running
    station (attach and detach). ``read_only`` lets a folder whose only
    missions are finished resolve to the finished one, so ``status`` and
    ``task list`` still work after completion.
    """
    if ref:
        exact = store.mission_id_if_exists(ref)
        if exact is not None:
            return exact
        if len(ref) < MIN_MISSION_PREFIX:
            raise ValueError(f"mission ID prefix {ref!r} is too short (use at least {MIN_MISSION_PREFIX} characters); run llm-bridge mission list")
        rows = store.missions_with_id_prefix(ref)
        if not rows:
            raise MissionNotFound(f"no mission matches {ref!r}; run llm-bridge mission list")
        if len(rows) > 1:
            raise ValueError(f"mission ID prefix {ref!r} is ambiguous; matches:\n{mission_choices(rows)}")
        return str(rows[0]["id"])
    here = Path(cwd if cwd is not None else Path.cwd()).expanduser().resolve()
    in_folder = [
        row
        for row in store.missions_newest_first()
        if here == Path(row["cwd"]) or Path(row["cwd"]) in here.parents
    ]
    candidates = [row for row in in_folder if row["status"] not in FINISHED_MISSION_STATUSES]
    live = [row for row in candidates if row["status"] in LIVE_MISSION_STATUSES]
    pool = live or candidates
    if prefer_station and len(pool) > 1:
        with_station = [row for row in pool if station_live(str(row["id"]))]
        pool = with_station or pool
    if len(pool) == 1:
        return str(pool[0]["id"])
    if not pool:
        finished = [row for row in in_folder if row["status"] in FINISHED_MISSION_STATUSES]
        if read_only and len(finished) == 1:
            return str(finished[0]["id"])
        if read_only and finished:
            raise ValueError(f"several finished missions match folder {here}; pass --mission ID:\n{mission_choices(finished)}")
        if finished:
            newest = order_missions(finished)[0]
            short = str(newest["id"])[:8]
            if newest["status"] == "COMPLETED":
                follow_up = f"add follow-up work with: llm-bridge task add --mission {short} --prompt \"...\" or create a new mission with: llm-bridge mission create --prompt \"...\""
            else:
                follow_up = "create a new mission with: llm-bridge mission create --prompt \"...\""
            raise ValueError(f"mission {short} in folder {here} is finished ({str(newest['status']).lower()}); {follow_up}")
        raise ValueError(f"no unfinished mission for folder {here}; pass --mission ID (see llm-bridge mission list) or create one with: llm-bridge mission create --prompt \"...\"")
    raise ValueError(f"several missions match folder {here}; pass --mission ID:\n{mission_choices(pool)}")
