"""Rigg — where agents are built, maintained, and dispatched.

Core SDK for governed AI agent orchestration. Products import this library
and bring their own infrastructure.
"""

__version__ = "0.1.0"

from rigg.agent_runner import (
    CLI_DISPATCH_SHAPE_FRAGMENT,
    DEFAULT_RUNNER_PREFIXES,
    AgentRunner,
    AgentRunnerError,
    ClaudeCodeRunner,
    ResolvedRunner,
    RunnerNotFound,
    clear_registry,
    register_runner,
    registered_prefixes_in_priority_order,
    resolve_runner,
    resolve_runner_by_name,
)
from rigg.blocking import BlockingProtocol, Notifier
from rigg.claude_agent_sdk_runner import ClaudeAgentSDKRunner
from rigg.git import (
    CredentialStrategy,
    GitRunner,
    TokenCredentialStrategy,
    WorktreeManager,
)
from rigg.models import (
    ActivityEvent,
    ActivityResult,
    AgentResult,
    BlockingReason,
    DispatchResult,
    Item,
    Proof,
    ReviewStatus,
    RunRecord,
    ValidationResult,
)
from rigg.orchestration import DispatchRule, ItemSource, OrchestrationEngine
from rigg.parsing import ParsedOutput, parse_claude_output
from rigg.prompt_registry import PromptNotFound, PromptRegistry
from rigg.proof_collector import ProofCollector, ProofValidator
from rigg.review_pipeline import MarkdownReviewParser, ReviewParser, ReviewPipeline
from rigg.state_machine import InvalidTransition, StateMachine, TransitionRule

__all__ = [
    # Models
    "ActivityEvent",
    "ActivityResult",
    "AgentResult",
    "BlockingReason",
    "DispatchResult",
    "Item",
    "Proof",
    "ReviewStatus",
    "RunRecord",
    "ValidationResult",
    # StateMachine
    "InvalidTransition",
    "StateMachine",
    "TransitionRule",
    # AgentRunner
    "AgentRunner",
    "AgentRunnerError",
    "ClaudeAgentSDKRunner",
    "ClaudeCodeRunner",
    "CLI_DISPATCH_SHAPE_FRAGMENT",
    # AgentRunner registry (ADR-023)
    "register_runner",
    "resolve_runner",
    "resolve_runner_by_name",
    "registered_prefixes_in_priority_order",
    "ResolvedRunner",
    "RunnerNotFound",
    "clear_registry",
    "DEFAULT_RUNNER_PREFIXES",
    # ReviewPipeline
    "MarkdownReviewParser",
    "ReviewParser",
    "ReviewPipeline",
    # Blocking
    "BlockingProtocol",
    "Notifier",
    # ProofCollector
    "ProofCollector",
    "ProofValidator",
    # PromptRegistry
    "PromptNotFound",
    "PromptRegistry",
    # Git
    "CredentialStrategy",
    "GitRunner",
    "TokenCredentialStrategy",
    "WorktreeManager",
    # Orchestration
    "DispatchRule",
    "ItemSource",
    "OrchestrationEngine",
    # Parsing
    "ParsedOutput",
    "parse_claude_output",
]
