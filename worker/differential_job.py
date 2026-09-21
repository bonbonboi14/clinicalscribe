from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import UUID

from core.diagnosis import DifferentialDiagnosisEngine, DifferentialResult
from core.models import ClinicalFact
from core.validation import HallucinationFirewall


def run_differential_job(
    session_id: UUID | str,
    facts: list[ClinicalFact],
    db_conn: sqlite3.Connection,
    *,
    session_enabled: bool | None = None,
    engine: DifferentialDiagnosisEngine | None = None,
) -> DifferentialResult:
    """Generate, validate, and append one enabled differential result."""
    selected_engine = engine or DifferentialDiagnosisEngine()
    result = selected_engine.generate(session_id, facts, session_enabled=session_enabled)
    if not result.enabled:
        return result
    HallucinationFirewall().validate_differential(result, facts)
    next_version = db_conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM differentials WHERE session_id = ?",
        (str(result.session_id),),
    ).fetchone()[0]
    db_conn.execute(
        "INSERT INTO differentials(id, session_id, version, enabled_at_generation, candidates_json, "
        "fact_ids_json, disclaimer, result_json, created_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
        (str(result.id), str(result.session_id), next_version,
         json.dumps([candidate.model_dump(mode="json") for candidate in result.candidates]),
         json.dumps([str(item) for item in result.fact_ids]), result.disclaimer,
         result.model_dump_json(), datetime.now(timezone.utc).isoformat()),
    )
    return result


__all__ = ["run_differential_job"]
