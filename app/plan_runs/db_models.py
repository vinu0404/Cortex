import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import Mapped, mapped_column

from database.session import Base


@dataclass
class PlanRunCreate:
    conversation_id: uuid.UUID
    message_id: Optional[uuid.UUID]
    user_query: str
    master_reasoning: str
    plan_dict: dict


class PlanRun(Base):
    __tablename__ = "plan_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    master_reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class AgentRunRecord(Base):
    __tablename__ = "agent_run_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plan_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    retry_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    input_task: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    input_tools: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    input_dependency_outputs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    output_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    output_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    time_taken_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class PlanRunModelService:
    async def create_plan_run(
        self,
        db: AsyncSession,
        payload: PlanRunCreate,
    ) -> PlanRun:
        plan_run = PlanRun(
            conversation_id=payload.conversation_id,
            message_id=payload.message_id,
            user_query=payload.user_query,
            master_reasoning=payload.master_reasoning,
            plan=payload.plan_dict,
        )
        db.add(plan_run)
        await db.flush()
        return plan_run

    async def create_agent_run_record(
        self,
        db: AsyncSession,
        plan_run_id: uuid.UUID,
        output,
        conversation_id: uuid.UUID,
    ) -> AgentRunRecord:
        meta = output.execution_metadata
        usage = output.resource_usage
        record = AgentRunRecord(
            plan_run_id=plan_run_id,
            conversation_id=conversation_id,
            agent_id=output.agent_id,
            agent_name=output.agent_name,
            retry_attempt=meta.get("retry_attempt", 0),
            input_task=meta.get("input_task", ""),
            input_tools=meta.get("tools", []),
            input_dependency_outputs=meta.get("dependency_outputs", {}),
            output_data=output.data,
            output_error=output.error,
            task_done=output.task_done,
            tokens_input=usage.get("input_tokens", 0),
            tokens_output=usage.get("output_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            time_taken_ms=usage.get("time_taken_ms", 0),
        )
        db.add(record)
        await db.flush()
        return record

    async def get_successful_agent_runs(
        self,
        db: AsyncSession,
        plan_run_id: uuid.UUID,
    ) -> list[AgentRunRecord]:
        result = await db.execute(
            select(AgentRunRecord)
            .where(AgentRunRecord.plan_run_id == plan_run_id, AgentRunRecord.task_done.is_(True))
            .order_by(AgentRunRecord.retry_attempt.asc())
        )
        return list(result.scalars().all())

    async def count_agent_retries(
        self,
        db: AsyncSession,
        plan_run_id: uuid.UUID,
        agent_id: str,
    ) -> int:
        result = await db.execute(
            select(AgentRunRecord)
            .where(AgentRunRecord.plan_run_id == plan_run_id, AgentRunRecord.agent_id == agent_id)
        )
        return len(result.scalars().all())
