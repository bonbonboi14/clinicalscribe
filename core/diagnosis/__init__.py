"""Optional, default-off differential decision support."""
"""Optional, transcript-grounded differential diagnosis decision support."""

from core.diagnosis.engine import DifferentialDiagnosisEngine
from models.differential import Differential, DifferentialCandidate, DifferentialResult, DxLikelihood

__all__ = [
    "Differential", "DifferentialCandidate", "DifferentialDiagnosisEngine",
    "DifferentialResult", "DxLikelihood",
]
