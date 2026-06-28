"""Tests for Rigg ProofCollector and ProofValidator."""

from rigg.proof_collector import ProofValidator

VALID_BODY = """## Summary
Changes look good.

## Proof
### API endpoint returns 200
```
$ curl localhost:8000/health
{"status": "ok"}
```

### Tests pass
```
$ make test
All 42 tests passed.
```
"""

MISSING_SECTION_BODY = """## Summary
Changes look good.

## Proof
### Tests pass
```
All tests passed.
```
"""

PLACEHOLDER_BODY = """## Summary
Changes look good.

## Proof
### API endpoint returns 200
TODO: add proof
"""


class TestProofValidator:
    def test_valid_body(self):
        v = ProofValidator()
        result = v.validate(VALID_BODY, required_sections=["API endpoint returns 200", "Tests pass"])
        assert result.valid is True
        assert result.missing_sections == []
        assert result.placeholder_detected is False

    def test_missing_section(self):
        v = ProofValidator()
        result = v.validate(
            MISSING_SECTION_BODY,
            required_sections=["API endpoint returns 200", "Tests pass"],
        )
        assert result.valid is False
        assert "API endpoint returns 200" in result.missing_sections

    def test_placeholder_detected(self):
        v = ProofValidator()
        result = v.validate(PLACEHOLDER_BODY, required_sections=["API endpoint returns 200"])
        assert result.valid is False
        assert result.placeholder_detected is True

    def test_empty_required_sections(self):
        v = ProofValidator()
        result = v.validate("anything", required_sections=[])
        assert result.valid is True

    def test_empty_body(self):
        v = ProofValidator()
        result = v.validate("", required_sections=["Something"])
        assert result.valid is False
        assert "Something" in result.missing_sections
