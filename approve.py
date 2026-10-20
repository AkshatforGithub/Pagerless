#!/usr/bin/env python3
"""CLI for managing safety approvals.

Subcommands:
  list               List all pending proposals
  show <id>          Show details of a pending proposal, including exact commands
  approve <id>       Approve and execute a pending proposal
  reject <id>        Reject a pending proposal
"""

import json
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
PENDING_DIR = RESULTS / "pending"


def _list_pending():
    """List all pending proposal IDs."""
    if not PENDING_DIR.exists():
        print("No pending proposals.")
        return
    files = sorted(PENDING_DIR.glob("*.json"))
    if not files:
        print("No pending proposals.")
        return
    for f in files:
        with open(f) as fp:
            rec = json.load(fp)
        print(f"{rec['id']}: action={rec['action']}, status={rec['status']}, "
              f"created={rec['created_at']}, investigation={rec['investigation_id']}")


def _show(id):
    """Show the exact commands from a pending proposal."""
    record = json.loads((PENDING_DIR / f"{id}.json").read_text())
    print(f"Proposal ID: {record['id']}")
    print(f"Action: {record['action']}")
    print(f"Params: {json.dumps(record['params'], indent=2)}")
    print(f"Created: {record['created_at']}")
    print(f"Status: {record['status']}")
    print(f"Investigation ID: {record['investigation_id']}")
    print("Exact commands:")
    for c in record["commands"]:
        print("  $", " ".join(c))


def _approve(id):
    """Approve and execute a pending proposal."""
    import actions
    from datetime import datetime, timedelta

    record = json.loads((PENDING_DIR / f"{id}.json").read_text())

    # Check status
    if record["status"] != "pending":
        print(f"Error: proposal {id} is not pending (status: {record['status']})")
        return

    # Check staleness (>15 min)
    created = datetime.fromisoformat(record["created_at"])
    now = datetime.now()
    if now - created > timedelta(minutes=15):
        print(f"Error: proposal {id} is older than 15 minutes")
        return

    # Check if destructive action and require typing the action name
    action = record["action"]
    destructive_actions = {"remove_container", "delete_data_file", "release_db_connections"}
    if action in destructive_actions:
        action_name = input(
            f"This will {action}. Type the action name to confirm: "
        ).strip()
        if action_name != action:
            print("Rejected: action name did not match.")
            return

    # Run approval via actions module
    try:
        result = actions.approve(id, approved_by="cli-user")
        print(f"Approved and executed proposal {id}.")
    except ValueError as e:
        print(f"Rejected: {e}")


def _reject(id):
    """Reject a pending proposal."""
    import actions
    try:
        actions.reject(id)
        print(f"Proposal {id} rejected.")
    except ValueError as e:
        print(f"Error: {e}")


def main():
    if len(sys.argv) < 2:
        print("Usage: approve.py <list|show|approve|reject> [id]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "list":
        _list_pending()
    elif cmd == "show":
        if len(sys.argv) < 3:
            print("Error: show requires an id argument.")
            sys.exit(1)
        _show(sys.argv[2])
    elif cmd == "approve":
        if len(sys.argv) < 3:
            print("Error: approve requires an id argument.")
            sys.exit(1)
        _approve(sys.argv[2])
    elif cmd == "reject":
        if len(sys.argv) < 3:
            print("Error: reject requires an id argument.")
            sys.exit(1)
        _reject(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: approve.py <list|show|approve|reject> [id]")


if __name__ == "__main__":
    main()