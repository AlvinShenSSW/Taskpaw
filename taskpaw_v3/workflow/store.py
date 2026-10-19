"""Opt-in workflow store. Construction is always explicit."""

from .approval_store import ApprovalCommands


class WorkflowStore(ApprovalCommands):
    """Public facade for transaction-owned workflow commands."""
