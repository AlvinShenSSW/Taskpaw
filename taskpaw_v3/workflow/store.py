"""Opt-in workflow store. Construction is always explicit."""

from .intake_store import IntakeCommands
from .route_store import RouteCommands


class WorkflowStore(IntakeCommands, RouteCommands):
    """Public facade for transaction-owned workflow commands."""
