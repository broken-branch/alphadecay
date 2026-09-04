from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ExperimentExecutionLineage:
    experiment_id: UUID
    source_definition_hash: str
    protocol_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.experiment_id, UUID) or not all(
            isinstance(value, str) and _HASH.fullmatch(value)
            for value in (self.source_definition_hash, self.protocol_hash)
        ):
            raise ValueError("EXPERIMENT_EXECUTION_LINEAGE_INVALID")

    def material(self) -> dict[str, str]:
        return {
            "experiment_id": str(self.experiment_id),
            "source_definition_hash": self.source_definition_hash,
            "protocol_hash": self.protocol_hash,
        }


def optional_experiment_execution_lineage(
    experiment_id: UUID | None,
    source_definition_hash: str | None,
    protocol_hash: str | None,
) -> ExperimentExecutionLineage | None:
    values = (experiment_id, source_definition_hash, protocol_hash)
    if all(value is None for value in values):
        return None
    if experiment_id is None or source_definition_hash is None or protocol_hash is None:
        raise ValueError("EXPERIMENT_EXECUTION_LINEAGE_INCOMPLETE")
    return ExperimentExecutionLineage(
        experiment_id=experiment_id,
        source_definition_hash=source_definition_hash,
        protocol_hash=protocol_hash,
    )
