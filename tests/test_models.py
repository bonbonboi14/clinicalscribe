from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.models import AssertionState, ClinicalFact, ExportRequest


def test_clinical_fact_requires_explicit_assertion() -> None:
    fact = ClinicalFact(
        session_id=uuid4(),
        category="symptom",
        name="chest pain",
        assertion=AssertionState.NEGATIVE,
        confidence=0.95,
    )
    assert fact.assertion is AssertionState.NEGATIVE


def test_export_requires_clinician_approval() -> None:
    with pytest.raises(ValidationError):
        ExportRequest(
            session_id=uuid4(),
            note_id=uuid4(),
            format="text",
            requested_by="clinician@example.test",
            clinician_approved=False,
        )

