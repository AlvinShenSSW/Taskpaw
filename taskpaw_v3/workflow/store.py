"""Opt-in workflow store. Construction is always explicit."""

from .intake_store import IntakeCommands


class WorkflowStore(IntakeCommands):
    """Public facade for transaction-owned workflow commands."""
