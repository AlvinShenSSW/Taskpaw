"""Execution credentials are independent of monitoring and local discovery.

Only digests cross into the store. Authentication proves possession; the store
must recheck current membership, generation, expiry and revocation in every
transaction, including an idempotent replay.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import SecretStr

from .models import WorkflowError

if TYPE_CHECKING:
    from .store import WorkflowStore

_SECRET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


@dataclass(frozen=True, repr=False)
class AuthProof:
    """An in-process authentication result, never a request body or bearer."""

    credential_id: str
    principal_id: str
    generation: int
    authority_id: str
    secret_digest: str


@dataclass(frozen=True, repr=False)
class PairingProof:
    """Possession of a ticket and proposed credential; only pairing consumes it."""

    pairing_id: str
    authority_id: str
    ticket_digest: str
    new_secret_digest: str


def digest_secret(
    secret: SecretStr, *, purpose: Literal["credential", "pairing"] = "credential"
) -> str:
    if not isinstance(secret, SecretStr) or purpose not in {"credential", "pairing"}:
        raise WorkflowError("unauthorized")
    raw = secret.get_secret_value()
    if not _SECRET.fullmatch(raw):
        raise WorkflowError("unauthorized")
    domain = f"TaskPaw/workflow/{purpose}/v1\0".encode("ascii")
    return hashlib.sha256(domain + raw.encode("ascii")).hexdigest()


def parse_bearer(header: str | None) -> tuple[str, SecretStr]:
    """The credential UUID is routing information, never an authorization claim."""
    if not isinstance(header, str) or not header.startswith("Bearer "):
        raise WorkflowError("unauthorized")
    parts = header[7:].split(".")
    if len(parts) != 2:
        raise WorkflowError("unauthorized")
    identity, raw = parts
    try:
        if str(UUID(identity)) != identity:
            raise ValueError
    except ValueError:
        raise WorkflowError("unauthorized") from None
    secret = SecretStr(raw)
    digest_secret(secret)
    return identity, secret


def authenticate(
    store: WorkflowStore, credential_id: str, secret: SecretStr
) -> AuthProof:
    return store._authenticate(credential_id, digest_secret(secret))


def authenticate_pairing(
    store: WorkflowStore,
    pairing_id: str,
    ticket_secret: SecretStr,
    new_secret: SecretStr,
) -> PairingProof:
    return store._authenticate_pairing(
        pairing_id,
        digest_secret(ticket_secret, purpose="pairing"),
        digest_secret(new_secret),
    )


# A fixed matrix: neither request bodies nor monitoring records grant scopes.
COMMAND_ROLES: dict[str, frozenset[str]] = {
    name: frozenset({"operator"})
    for name in (
        "grant_component",
        "revoke_credential",
        "register_worker",
        "update_worker",
        "issue_pairing",
        "revoke_worker",
        "retire_worker",
        "mark_unknown",
        "resolve_assignment",
        "retry_stage",
        "skip_stage",
        "put_approval_context",
        "approve",
        "revoke_approval",
        "begin_archive",
        "begin_cleanup",
        "configure_intake_root",
        "resolve_intake_grouping",
    )
}
COMMAND_ROLES.update(
    {
        "complete_pairing": frozenset({"compute_worker"}),
        "record_capabilities": frozenset({"operator", "compute_worker"}),
        "record_boot": frozenset({"compute_worker"}),
        "create_film": frozenset({"intake_adapter"}),
        "put_input_manifest": frozenset({"intake_adapter"}),
        "observe_intake": frozenset({"intake_adapter"}),
        "record_intake_source_notice": frozenset({"intake_adapter"}),
        "begin_intake_root_scan": frozenset({"intake_adapter"}),
        "finish_intake_root_scan": frozenset({"intake_adapter"}),
        "mark_intake_scan_seen": frozenset({"intake_adapter"}),
        "begin_intake_snapshot": frozenset({"intake_adapter"}),
        "record_intake_owner": frozenset({"intake_adapter"}),
        "finish_intake_snapshot": frozenset({"intake_adapter"}),
        "cancel_intake_snapshot": frozenset({"intake_adapter", "operator"}),
        "put_final_manifest": frozenset({"scrape_adapter"}),
        "claim": frozenset({"compute_worker", "receiver_adapter", "scrape_adapter"}),
        "renew": frozenset({"compute_worker", "receiver_adapter", "scrape_adapter"}),
        "finish_attempt": frozenset(
            {"compute_worker", "receiver_adapter", "scrape_adapter"}
        ),
        "accept_artifact_receipt": frozenset({"receiver_adapter"}),
        "accept_archive_receipt": frozenset({"archive_adapter"}),
        "record_cleanup_result": frozenset({"cleanup_adapter"}),
    }
)

STAGE_ROLES: dict[str, str] = {
    "restore": "compute_worker",
    "asr": "compute_worker",
    "translate": "compute_worker",
    "return": "receiver_adapter",
    "scrape": "scrape_adapter",
}


def require_role(role: str, command: str) -> None:
    if role not in COMMAND_ROLES.get(command, frozenset()):
        raise WorkflowError("forbidden")
