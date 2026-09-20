"""Clinical Examination Interpreter; preserves source phrases and confidence."""

from core.examination.interpreter import ExaminationInterpreter
from core.examination.mapper import ExaminationMapper, MappingResult, PhraseToClinicalTermMapper

__all__ = ["ExaminationInterpreter", "ExaminationMapper", "MappingResult", "PhraseToClinicalTermMapper"]

