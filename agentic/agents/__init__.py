from .analyst import CodebaseAnalystAgent
from .architect import ArchitectAgent
from .base import Agent, AgentContext, AgentError, AgentResult, NodeSpec
from .implementer import ImplementerAgent, TestAgent
from .release import ReleaseAgent
from .requirements import RequirementsAgent
from .security import SecurityAgent

# Seven agents, one per SDLC role:
#   requirements -> analyst (brownfield) / security (threat model) -> architect (design + plan)
#   -> implementer (per task) -> tester -> security (review) -> release (docs + go/no-go)
REGISTRY: dict[str, Agent] = {
    a.name: a
    for a in (
        RequirementsAgent(), CodebaseAnalystAgent(), SecurityAgent(), ArchitectAgent(),
        ImplementerAgent(), TestAgent(), ReleaseAgent(),
    )
}

__all__ = ["REGISTRY", "Agent", "AgentContext", "AgentError", "AgentResult", "NodeSpec"]
