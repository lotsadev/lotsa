"""Test that the Rigg public API exports the expected symbols."""

import sys

import rigg


def test_version():
    assert rigg.__version__ == "0.1.0"


def test_core_exports():
    expected = [
        "AgentResult",
        "AgentRunner",
        "AgentRunnerError",
        "BlockingProtocol",
        "BlockingReason",
        "ClaudeCodeRunner",
        "CredentialStrategy",
        "DispatchResult",
        "DispatchRule",
        "GitRunner",
        "InvalidTransition",
        "Item",
        "ItemSource",
        "MarkdownReviewParser",
        "Notifier",
        "OrchestrationEngine",
        "Proof",
        "ProofCollector",
        "ProofValidator",
        "PromptNotFound",
        "PromptRegistry",
        "ReviewParser",
        "ReviewPipeline",
        "ReviewStatus",
        "RunRecord",
        "StateMachine",
        "TokenCredentialStrategy",
        "TransitionRule",
        "ValidationResult",
    ]
    for name in expected:
        assert hasattr(rigg, name), f"rigg missing export: {name}"
        assert getattr(rigg, name) is not None


def test_no_infrastructure_imports():
    """Rigg should not import from bot/, api/, or lotsa_sdk/."""
    rigg_modules = [m for m in sys.modules if m.startswith("rigg")]
    for mod_name in rigg_modules:
        mod = sys.modules[mod_name]
        if mod is None:
            continue
        source = getattr(mod, "__file__", "") or ""
        assert "/bot/" not in source
        assert "/api/" not in source
        assert "/lotsa_sdk/" not in source
