from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from backend.app.contracts.v1 import BaselineStatus, MeasurementStatus, PerformancePoint

FINAL_PERFORMANCE_BOUNDARY = datetime(2026, 9, 4, 14, 30, tzinfo=UTC)
FINAL_PUBLICATION_NOT_BEFORE = datetime(2026, 9, 4, 14, 35, tzinfo=UTC)
FINAL_PERFORMANCE_BOUNDARY_KEY = "FINAL_2026-09-04T14:30:00Z"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PerformanceSnapshot:
    snapshot_id: UUID
    submission_baseline_id: UUID | None
    boundary_key: str
    point: PerformancePoint
    baseline_status: BaselineStatus
    account_fingerprint: str
    positions_manifest_hash: str | None
    orders_manifest_hash: str | None
    activities_manifest_hash: str | None
    snapshot_hash: str

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: UUID,
        submission_baseline_id: UUID | None,
        boundary_key: str,
        point: PerformancePoint,
        baseline_status: BaselineStatus,
        account_fingerprint: str,
        positions_manifest_hash: str | None,
        orders_manifest_hash: str | None,
        activities_manifest_hash: str | None,
    ) -> PerformanceSnapshot:
        manifest_hashes = (
            positions_manifest_hash,
            orders_manifest_hash,
            activities_manifest_hash,
        )
        if not boundary_key:
            raise ValueError("performance boundary key is required")
        is_final_key = boundary_key == FINAL_PERFORMANCE_BOUNDARY_KEY
        is_final_time = point.scheduled_for == FINAL_PERFORMANCE_BOUNDARY
        if is_final_key != is_final_time:
            raise ValueError("final performance boundary key and time must match")
        private_hashes = (account_fingerprint, *manifest_hashes)
        if any(
            value is not None and SHA256_PATTERN.fullmatch(value) is None
            for value in private_hashes
        ):
            raise ValueError("private reconciliation hashes must be lowercase SHA-256")
        if point.status == MeasurementStatus.COMPLETE and any(
            value is None for value in manifest_hashes
        ):
            raise ValueError("complete snapshot requires private reconciliation manifests")
        if point.status != MeasurementStatus.COMPLETE and any(
            value is not None for value in manifest_hashes
        ):
            raise ValueError("incomplete snapshot cannot fabricate private manifests")
        material = {
            "snapshot_id": str(snapshot_id),
            "submission_baseline_id": (
                None if submission_baseline_id is None else str(submission_baseline_id)
            ),
            "boundary_key": boundary_key,
            "point": point.model_dump(mode="json"),
            "baseline_status": baseline_status.value,
            "account_fingerprint": account_fingerprint,
            "positions_manifest_hash": positions_manifest_hash,
            "orders_manifest_hash": orders_manifest_hash,
            "activities_manifest_hash": activities_manifest_hash,
        }
        return cls(
            snapshot_id=snapshot_id,
            submission_baseline_id=submission_baseline_id,
            boundary_key=boundary_key,
            point=point,
            baseline_status=baseline_status,
            account_fingerprint=account_fingerprint,
            positions_manifest_hash=positions_manifest_hash,
            orders_manifest_hash=orders_manifest_hash,
            activities_manifest_hash=activities_manifest_hash,
            snapshot_hash=canonical_hash(material),
        )


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
