"""The approval policy: pure code, so it is testable and never depends on what a model says."""
from .settings import Policy


def required_approvals(price: float, policy: Policy) -> int:
    """How many different humans must approve a booking at this price: 0 (automatic), 1, or 2."""
    if price < policy.auto_approve_below:
        return 0
    if price >= policy.second_approver_from:
        return 2
    return 1


def describe(policy: Policy) -> dict:
    """The policy as the API and the web UI show it."""
    return {
        "rules": [
            {"price_below_eur": policy.auto_approve_below, "approvals": 0, "meaning": "booked automatically"},
            {"price_from_eur": policy.auto_approve_below, "price_below_eur": policy.second_approver_from, "approvals": 1,
             "meaning": "one approver"},
            {"price_from_eur": policy.second_approver_from, "approvals": 2, "meaning": "two different approvers"},
        ],
        "approval_ttl_seconds": policy.approval_ttl_seconds,
        "escalate_after_seconds": policy.escalate_after_seconds,
        "max_edits": policy.max_edits,
        "self_approval_allowed": policy.allow_self_approval,
    }
