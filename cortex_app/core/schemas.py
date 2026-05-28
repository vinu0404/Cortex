from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class LongTermMemory(BaseModel):
    critical_facts: dict[str, Any] = Field(default_factory=dict)
    preferences: dict[str, Any] = Field(default_factory=dict)


class HitlResolvedDecision(BaseModel):
    request_id: str
    approved: bool
    instructions: str | None = None


class ArtifactPreview(BaseModel):
    type: str  # "text", "table", "code", "url"
    title: str
    content: Any


class AgentInput(BaseModel):
    agent_id: str
    agent_name: str
    task: str
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    long_term_memory: LongTermMemory = Field(default_factory=LongTermMemory)
    dependency_outputs: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    retry_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    hitl_context: Optional[HitlResolvedDecision] = None


class AgentOutput(BaseModel):
    agent_id: str
    agent_name: str
    task_description: str
    task_done: bool
    data: Optional[dict[str, Any]] = None
    partial_data: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    confidence_score: float = 1.0
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactPreview] = Field(default_factory=list)


class PlanStep(BaseModel):
    agent_id: str
    agent_name: str
    task: str
    depends_on: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    steps: list[PlanStep]
    reasoning: str = ""


class ResolvedAgentTask(BaseModel):
    agent_id: str
    agent_name: str
    task: str
    depends_on: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class LongTermMemoryExtraction(BaseModel):
    should_store: bool
    critical_facts: dict[str, Any] = Field(default_factory=dict)
    preferences: dict[str, Any] = Field(default_factory=dict)


class TitleGenerationOutput(BaseModel):
    title: str


class SuggestionsOutput(BaseModel):
    questions: list[str]


class MemoryCompressionOutput(BaseModel):
    summary: str
    key_points: list[str] = Field(default_factory=list)
