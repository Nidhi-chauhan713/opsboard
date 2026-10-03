"""Work-item state machine.

The machine is data (a dict of allowed edges) plus one guard function per edge.
Keeping it in one place means every rule can be read, tested and explained
without hunting through route handlers.

    open ──▶ in_progress ──▶ resolved ──▶ closed
     ▲  ◀──       │  ▲          │           │
     │            ▼  │          ▼ (reopen)  │ (lead reopen)
     │     awaiting_approval   in_progress  ▼
     │        │         │                  open
     │        ▼         ▼
     │     approved  rejected ──▶ in_progress
     │        │
     │        ▼
     └──── resolved
"""
import sqlite3

from .auth import CurrentUser
from .errors import ApiError

TRANSITIONS: dict[str, set[str]] = {
    "open": {"in_progress"},
    "in_progress": {"open", "awaiting_approval", "resolved"},
    "awaiting_approval": {"approved", "rejected"},
    "approved": {"resolved"},
    "rejected": {"in_progress"},
    "resolved": {"closed", "in_progress"},
    "closed": {"open"},
}

ACTIVE_STATUSES = ("open", "in_progress", "awaiting_approval", "approved", "rejected")


def _deny(message: str) -> ApiError:
    return ApiError(422, "workflow_violation", message)


def check_transition(user: CurrentUser, item: sqlite3.Row, to: str, comment: str | None) -> None:
    """Raise ApiError if `user` may not move `item` to status `to`. Pure: no DB writes."""
    frm = item["status"]
    if to not in TRANSITIONS.get(frm, set()):
        raise _deny(f"Cannot move from '{frm}' to '{to}'. Allowed: {sorted(TRANSITIONS.get(frm, set()))}")

    team = item["team_id"]
    is_lead = user.has_role(team, "lead")
    is_assignee = item["assignee_id"] == user.id
    is_creator = item["created_by"] == user.id

    if not user.has_role(team, "member"):
        raise ApiError(403, "forbidden", "Viewers cannot change workflow state")

    if frm == "open" and to == "in_progress":
        if item["assignee_id"] is None:
            raise _deny("Item must have an owner before work starts — claim it first")
        if not (is_assignee or is_lead):
            raise ApiError(403, "forbidden", "Only the owner or a team lead can start work")

    elif frm == "in_progress" and to == "awaiting_approval":
        if not item["requires_approval"]:
            raise _deny("This item does not require approval; resolve it directly")
        if not (is_assignee or is_lead):
            raise ApiError(403, "forbidden", "Only the owner or a team lead can request approval")

    elif frm == "in_progress" and to == "resolved":
        if item["requires_approval"]:
            raise _deny("This item requires approval before it can be resolved")
        if not (is_assignee or is_lead):
            raise ApiError(403, "forbidden", "Only the owner or a team lead can resolve")

    elif frm == "awaiting_approval" and to in ("approved", "rejected"):
        if not is_lead:
            raise ApiError(403, "forbidden", "Only a team lead can approve or reject")
        # Separation of duties: nobody approves their own request (admins included).
        if item["approval_requested_by"] == user.id:
            raise _deny("You cannot approve or reject an approval you requested")
        if to == "rejected" and not (comment and comment.strip()):
            raise _deny("A rejection must include a reason (comment)")

    elif frm == "resolved" and to == "closed":
        if not (is_lead or is_creator):
            raise ApiError(403, "forbidden", "Only the requester or a team lead can close")

    elif frm == "resolved" and to == "in_progress":
        if not (is_lead or is_creator or is_assignee):
            raise ApiError(403, "forbidden", "Only the requester, owner or a lead can reopen")

    elif frm == "closed" and to == "open":
        if not is_lead:
            raise ApiError(403, "forbidden", "Only a team lead can reopen a closed item")

    else:  # in_progress->open, approved->resolved, rejected->in_progress
        if not (is_assignee or is_lead):
            raise ApiError(403, "forbidden", "Only the owner or a team lead can do this")
