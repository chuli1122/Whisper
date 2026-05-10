from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ_EAST8 = timezone(timedelta(hours=8))


def _now_beijing() -> datetime:
    return datetime.now(TZ_EAST8)
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declarative_base, mapped_column
from pgvector.sqlalchemy import Vector

Base = declarative_base()


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta_info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    summary_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    telegram_message_id: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    qq_message_id: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    wechat_message_id: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    image_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assistant_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class SessionSummary(Base):
    __tablename__ = "session_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.id"), index=True)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=True, index=True)
    summary_content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    perspective: Mapped[str] = mapped_column(String(100), nullable=False)
    msg_id_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    msg_id_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mood_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merged_into: Mapped[str | None] = mapped_column(String(20), nullable=True)
    merged_at_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SummaryLayer(Base):
    __tablename__ = "summary_layers"
    __table_args__ = (
        UniqueConstraint("assistant_id", "layer_type", name="uq_summary_layers_assistant_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assistant_id: Mapped[int] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=False, index=True)
    layer_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    time_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    needs_merge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class SummaryLayerHistory(Base):
    __tablename__ = "summary_layer_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary_layer_id: Mapped[int] = mapped_column(Integer, ForeignKey("summary_layers.id"), index=True)
    layer_type: Mapped[str] = mapped_column(String(20), nullable=False)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    merged_summary_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class CoreBlock(Base):
    __tablename__ = "core_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class CoreBlockCandidate(Base):
    __tablename__ = "core_block_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_summary_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("session_summaries.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class CoreBlockHistory(Base):
    __tablename__ = "core_block_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    core_block_id: Mapped[int] = mapped_column(Integer, ForeignKey("core_blocks.id"), index=True)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class WorldBook(Base):
    __tablename__ = "world_books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    activation: Mapped[str] = mapped_column(String(16), nullable=False, default="always")
    keywords: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True, default=list)
    message_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    folder: Mapped[str | None] = mapped_column(String(100), nullable=True)
    xml_tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    klass: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    manual_boost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    halflife_days: Mapped[float] = mapped_column(Float, nullable=False, default=60.0)
    last_access_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    is_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class MemoryVersion(Base):
    __tablename__ = "memory_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_id: Mapped[int] = mapped_column(Integer, ForeignKey("memories.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    klass: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tags: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class SummaryVersion(Base):
    __tablename__ = "summary_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary_id: Mapped[int] = mapped_column(Integer, ForeignKey("session_summaries.id"), nullable=False, index=True)
    summary_content: Mapped[str] = mapped_column(Text, nullable=False)
    mood_tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class ReflectionLog(Base):
    __tablename__ = "reflection_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_count: Mapped[int] = mapped_column(Integer, nullable=False)
    changes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class PendingReflectionChange(Base):
    __tablename__ = "pending_reflection_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reflection_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # update / delete / merge
    memory_id: Mapped[int] = mapped_column(Integer, nullable=False)
    merge_into_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Proposed new values
    proposed_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_klass: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposed_disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_tags: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Snapshot of old values
    old_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_klass: Mapped[str | None] = mapped_column(String(32), nullable=True)
    old_disclosure: Mapped[str | None] = mapped_column(Text, nullable=True)
    merge_target_old_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Status: pending / confirmed / rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class PendingMemory(Base):
    __tablename__ = "pending_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Link to the real Memory entry (new architecture)
    memory_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memories.id"), nullable=True)
    # Legacy fields (kept for old data, new entries use Memory directly)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    klass: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    # Related existing memory (similar/conflicting)
    related_memory_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("memories.id"), nullable=True)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Source
    summary_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("session_summaries.id"), nullable=True)
    # Status: pending / confirmed / dismissed / auto_resolved
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class Diary(Base):
    __tablename__ = "diary"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assistant_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=True, index=True)
    author: Mapped[str] = mapped_column(String(16), nullable=False, default="assistant")  # "assistant" | "user"
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unlock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class ApiProvider(Base):
    __tablename__ = "api_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(50), nullable=False, default="api_key")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class ModelPreset(Base):
    __tablename__ = "model_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=2048)
    thinking_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thinking_keyword: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    api_provider_id: Mapped[int] = mapped_column(Integer, ForeignKey("api_providers.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model_preset_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("model_presets.id"), nullable=True, index=True)
    summary_model_preset_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("model_presets.id"), nullable=True, index=True)
    summary_fallback_preset_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("model_presets.id"), nullable=True, index=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserProfile(Base):
    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    basic_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    nickname: Mapped[str | None] = mapped_column(String(100), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    background_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    theme: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class TheaterCard(Base):
    __tablename__ = "theater_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    setting: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_set_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class TheaterStory(Base):
    __tablename__ = "theater_stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[int] = mapped_column(Integer, ForeignKey("theater_cards.id"), index=True)
    ai_partner: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    story_timespan: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class ProactiveReminder(Base):
    __tablename__ = "proactive_reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assistant_id: Mapped[int] = mapped_column(Integer, ForeignKey("assistants.id"), nullable=False, default=2)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class CotRecord(Base):
    __tablename__ = "cot_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    round_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assistant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class IosCommand(Base):
    __tablename__ = "ios_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class IosReport(Base):
    __tablename__ = "ios_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing, index=True)


class YoruMemory(Base):
    __tablename__ = "yoru_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class YoruChat(Base):
    __tablename__ = "yoru_chat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing, index=True)


class RinMemory(Base):
    __tablename__ = "rin_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)


class RinChat(Base):
    __tablename__ = "rin_chat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing, index=True)
