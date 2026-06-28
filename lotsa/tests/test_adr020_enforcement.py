"""Static enforcement (ADR-020 Phase 2): ``claim_task_transition`` is fenced
to ``lotsa/db.py``.

``TaskDB.claim_task_transition`` stays *defined* in ``lotsa/db.py`` — it is
the raw CAS primitive that ``TaskDB.atomic_transition`` delegates to. But
every **production** call site must go through ``atomic_transition``, which
writes the paired audit row in the same transaction as the CAS. A direct
``claim_task_transition`` call from any other production module reintroduces
the audit-drift failure mode ADR-020 set out to close.

This test acts as a CI lint rule: it fails (with the offending file:line
list) if any production module under ``lotsa/`` names
``claim_task_transition`` outside ``lotsa/db.py``.

Test modules are exempt by design (ADR-020 Phase 2 plan, Correction B): they
exercise the primitive directly as a state-setup fixture, and one of them
(``test_status_model.py``) monkeypatches it to intercept the CAS — neither
can be migrated to ``atomic_transition``.
"""

from __future__ import annotations

from pathlib import Path

# This file lives at lotsa/tests/test_adr020_enforcement.py, so parent.parent
# is the lotsa/ package directory.
_LOTSA_DIR = Path(__file__).resolve().parent.parent

# The one production module allowed to name claim_task_transition.
_ALLOWED = _LOTSA_DIR / "db.py"

_NEEDLE = "claim_task_transition"


def _production_py_files() -> list[Path]:
    """Every ``*.py`` under ``lotsa/`` except ``db.py`` and test modules."""
    files: list[Path] = []
    for path in _LOTSA_DIR.rglob("*.py"):
        if path == _ALLOWED:
            continue
        if "tests" in path.relative_to(_LOTSA_DIR).parts:
            continue  # test modules may call/intercept the primitive directly
        files.append(path)
    return files


def _violations() -> list[tuple[Path, int, str]]:
    """Return (file, line_no, stripped_line) for each out-of-bounds reference.

    Comment lines (``#`` after lstrip) are ignored — only live references
    count.
    """
    out: list[tuple[Path, int, str]] = []
    for path in _production_py_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, start=1):
            if _NEEDLE in line and not line.lstrip().startswith("#"):
                out.append((path, lineno, line.strip()))
    return out


def test_lotsa_dir_and_db_module_resolve() -> None:
    """Guard the path assumptions so a real failure can't be masked by a
    mis-resolved root (e.g. the test silently scanning zero files)."""
    assert _LOTSA_DIR.name == "lotsa"
    assert _ALLOWED.is_file()
    assert _production_py_files(), "expected to find production modules under lotsa/"


def test_claim_task_transition_fenced_to_db_module() -> None:
    """No production module outside ``lotsa/db.py`` may call
    ``claim_task_transition`` directly.

    Migrate any failing call site to ``TaskDB.atomic_transition`` (ADR-020
    Phase 2).
    """
    violations = _violations()
    if violations:
        listing = "\n  ".join(f"{p}:{n}: {text}" for p, n, text in violations)
        raise AssertionError(
            "claim_task_transition called outside lotsa/db.py at:\n  "
            f"{listing}\n"
            "Migrate each site to TaskDB.atomic_transition (see ADR-020 Phase 2)."
        )
