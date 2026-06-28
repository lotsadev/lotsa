"""Pluggable proof collection for acceptance criteria.

Extracted from: bot/scripts/collect_api_proof.py, collect_visual_proof.py,
validate_pr_proof.py.
"""

from __future__ import annotations

import re
from typing import Protocol

from rigg.models import Proof, ValidationResult

# Patterns that indicate placeholder/incomplete proof
_PLACEHOLDER_PATTERNS = [
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bPLACEHOLDER\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
]


class ProofCollector(Protocol):
    """Protocol for collecting proof of acceptance criteria."""

    async def collect(self, criterion: dict, context: dict) -> Proof: ...


class ProofValidator:
    """Validate that a document body has required proof sections."""

    def validate(self, body: str, required_sections: list[str]) -> ValidationResult:
        missing = []
        for section in required_sections:
            # Look for the section as a markdown heading (any level)
            pattern = re.compile(rf"^#{{1,6}}\s+{re.escape(section)}\s*$", re.MULTILINE)
            if not pattern.search(body):
                missing.append(section)

        placeholder_detected = any(p.search(body) for p in _PLACEHOLDER_PATTERNS)

        valid = len(missing) == 0 and not placeholder_detected
        details_parts = []
        if missing:
            details_parts.append(f"Missing sections: {', '.join(missing)}")
        if placeholder_detected:
            details_parts.append("Placeholder text detected")

        return ValidationResult(
            valid=valid,
            missing_sections=missing,
            placeholder_detected=placeholder_detected,
            details="; ".join(details_parts) if details_parts else "All sections present",
        )
