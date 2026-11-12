"""Human-readable printing of missions, events, tasks and results for the Bridge CLI."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable
from . import bridge_commands as commands
from . import bridge_format as fmt
import argparse
import shutil
from .bridge import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    MISSION_LIST_PAGE,
    _fail,
    _json_print,
    open_store,
)


def _proposal_page(args: argparse.Namespace, result: dict[str, Any]) -> dict[str, Any]:
    """``proposal list`` result with ``total``: how many proposals match, so a full page can say so."""
    proposals = result.get("proposals") or []
    with open_store(readonly=True) as store:
        after_id = int(store.db.execute("SELECT COALESCE(MAX(id),0) FROM tasks WHERE mission_id=? AND ordinal<=?", (args.mission, max(0, args.after))).fetchone()[0])
        clause = "" if args.status == "ALL" else " AND proposal_status=?"
        params: tuple[Any, ...] = (args.mission, after_id, *(() if args.status == "ALL" else (args.status,)))
        total = int(store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=? AND origin='agent-proposal' AND id>?" + clause, params).fetchone()[0])
    return {**result, "total": max(total, len(proposals))}


def _print_missions(args: argparse.Namespace) -> int:
    """``missions`` and ``mission list``: every mission, live ones first, at most ``--limit`` of them."""
    from . import bridge_missions, bridge_switcher

    with open_store(readonly=True) as store:
        # One liveness probe for every mission listing and ambiguity message (tests that stub liveness patch it).
        entries = bridge_switcher.collect_entries(store, station_probe=lambda mission_id: bridge_missions.station_live(mission_id))
    total = len(entries)
    limit = args.limit if args.limit is not None else (None if args.json else MISSION_LIST_PAGE)
    page = entries if limit is None else entries[:max(0, limit)]
    if args.json:
        _json_print(bridge_switcher.missions_json(page, total))
    else:
        print(bridge_switcher.format_missions(page, total=total))
    return 0


def _events_command(args: argparse.Namespace) -> int:
    """``events``: the newest events (or the first ones after ``--after``), then new ones with ``--follow``."""
    newest = args.after is None
    after = args.after or 0

    def fetch(cursor: int, *, latest: bool, limit: int, hide: bool, since: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"after": cursor, "limit": limit}
        if latest:
            params["newest"] = True
        if since:
            params["since_seconds"] = since
        if hide:
            params["hide_bookkeeping"] = True
        return commands.dispatch(commands.CommandRequest("mission.events", params, mission_id=args.mission))

    # Everything the mission has recorded so far, shown or not, so --follow starts after it.
    seen = 0
    if args.follow:
        latest_any = fetch(0, latest=True, limit=1, hide=False)["events"]
        seen = int(latest_any[-1]["id"]) if latest_any else 0
    result = fetch(after, latest=newest, limit=args.limit, hide=not args.verbose, since=args.since)
    events = result["events"]
    if args.json and not args.follow:
        _json_print(result)
        return 0
    if args.json:
        for event in events:
            _json_print(event, compact=True)
    else:
        print(fmt.format_events(events, total=result.get("total"), newest=newest, verbose=args.verbose, after=after))
    if not args.follow:
        return 0
    printed = max([seen, *(int(event["id"]) for event in events)])
    cursor = printed
    try:
        while True:
            time.sleep(1.0)
            page = fetch(cursor, latest=False, limit=1000, hide=not args.verbose)["events"]
            fresh = [event for event in page if int(event["id"]) > printed]
            if page:
                cursor = int(page[-1]["id"])
            for event in fresh:
                if args.json:
                    _json_print(event, compact=True)
                else:
                    print(fmt.format_event_line(event, verbose=args.verbose))
            sys.stdout.flush()
            if fresh:
                printed = int(fresh[-1]["id"])
    except KeyboardInterrupt:
        return 0


def _print_result(result: Any, as_json: bool, human: Callable[[Any], str] | None = None) -> None:
    """Print ``result``: stable sorted JSON for ``--json``, otherwise readable text."""
    if as_json:
        _json_print(result)
    elif isinstance(result, str):
        print(result)
    elif human is not None:
        print(human(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def _task_text(args: argparse.Namespace) -> Callable[[Any], str]:
    action = args.task_action
    if action == "list":
        return fmt.format_task_list
    if action == "show":
        return fmt.format_task_detail
    return lambda value: fmt.format_task_result(action, value)


def _print_task_evidence(args: argparse.Namespace, result: dict[str, Any]) -> int:
    """Print `task output` or `task diff`: JSON, plain text, or the log in the document viewer on a terminal."""
    available = bool(result.get("available"))
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK if available else EXIT_NOT_FOUND
    if not available:
        return _fail(str(result.get("message")), "not-found", EXIT_NOT_FOUND, False)
    if args.task_action == "diff":
        sys.stdout.write(str(result["text"]))
        return 0
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if args.follow and not interactive:
        raise ValueError("--follow needs an interactive terminal; omit it to print the log")
    path = Path(str(result["path"]))
    if interactive:
        from .bridge_viewer import MAX_SOURCE_BYTES, run_document

        title = f"Output · task {result.get('task')} · {result.get('provider') or 'provider'}"
        if args.follow:
            return run_document("", title=title, path=path, follow=True)
        return run_document(path.read_bytes()[-MAX_SOURCE_BYTES:].decode("utf-8", "replace"), title=title)
    import shutil

    with path.open("rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
    sys.stdout.buffer.flush()
    return 0


def _proposal_text(args: argparse.Namespace) -> Callable[[Any], str]:
    if args.proposal_action == "list":
        return lambda value: fmt.format_proposals(value["proposals"], total=value.get("total"))
    return lambda value: fmt.format_proposal_result(args.proposal_action, value)
