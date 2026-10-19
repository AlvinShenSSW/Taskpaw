"""Local discovery invalidation commands, separate from the immutable snapshot."""

from typing import Literal

from pydantic import model_validator
from typing_extensions import Self

from .intake_models import Reason
from .models import (
    Count,
    Id,
    Model,
    NativeIdentity,
    RelativePath,
    Revision,
    UtcTime,
    VersionedCommand,
)


class IntakeLookup(Model):
    root_id: Id
    relative_path: RelativePath
    native_identity: NativeIdentity | None = None


class IntakeScanSeen(VersionedCommand):
    package_id: Id
    root_id: Id
    root_config_revision: Revision
    scan_id: Id


class IntakeSourceNotice(VersionedCommand):
    package_id: Id
    root_config_revision: Revision
    source_generation: Revision
    observed_at: UtcTime
    kind: Literal["dirty", "missing"]
    trigger: Literal["notification", "overflow", "startup", "complete_rescan"]

    @model_validator(mode="after")
    def missing_coverage(self) -> Self:
        if self.kind == "missing" and self.trigger != "complete_rescan":
            raise ValueError("complete_root_scan_required")
        return self


class IntakeRootScanBegin(Model):
    root_id: Id
    root_config_revision: Revision
    expected_version: Count
    scan_id: Id
    observed_at: UtcTime
    trigger: Literal["startup", "notification", "overflow", "rescan", "reconnect"]


class IntakeRootScanFinish(VersionedCommand):
    root_id: Id
    root_config_revision: Revision
    scan_id: Id
    observed_at: UtcTime
    complete: bool
    reason: Reason


class IntakeRootScanState(Model):
    root_id: Id
    root_config_revision: Revision
    version: Revision
    scan_id: Id
    required: bool
    reason: Reason
    observed_at: UtcTime
    last_complete_scan_at: UtcTime | None
