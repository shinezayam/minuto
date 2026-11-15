"""0001 baseline-ийн ЗАЛГАСАН (frozen) схем. Моделиас хамаарахгүй — модель хожим өөрчлөгдөхөд энэ файл өөрчлөгдөхгүй.

Шинэ өөрчлөлт бүр тусдаа revision байна. Энэ файлыг ШИНЭЧИЛЖ БОЛОХГҮЙ.
"""
from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()

Table(
    "users", metadata,
    Column("id", String(36), primary_key=True),
    Column("email", String(320), unique=True, index=True, nullable=False),
    Column("password_hash", String(255), nullable=False),
    Column("name", String(100), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Table(
    "workspaces", metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("description", Text, nullable=True),
    Column("workspace_type", String(20), nullable=False),
    Column("owner_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Table(
    "projects", metadata,
    Column("id", String(36), primary_key=True),
    Column("workspace_id", String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("name", String(100), nullable=False),
    Column("description", Text, nullable=True),
    Column("color", String(7), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Table(
    "meetings", metadata,
    Column("id", String(36), primary_key=True),
    Column("owner_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("title", String(200), nullable=False),
    Column("status", String(20), nullable=False),
    Column("language", String(5), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("duration_ms", BigInteger, nullable=False),
    Column("pause_accumulated_ms", BigInteger, nullable=False),
    Column("last_resume_ms", BigInteger, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("processing_stage", String(20), nullable=True),
    Column("processing_done", Integer, nullable=False),
    Column("processing_total", Integer, nullable=False),
    Column("processing_error", Text, nullable=True),
    Column("transcript", JSON, nullable=True),
    Column("minutes", JSON, nullable=True),
    Column("participants", JSON, nullable=True),
    Column("archived", Integer, nullable=False),
    Column("template_id", String(36), nullable=True),
    Column("workspace_id", String(36), ForeignKey("workspaces.id", ondelete="SET NULL"), index=True, nullable=True),
    Column("project_id", String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True, nullable=True),
)
Table(
    "contacts", metadata,
    Column("id", String(36), primary_key=True),
    Column("owner_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("name", String(100), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("owner_id", "name", name="uq_contact_owner_name"),
)
Table(
    "vocabulary_terms", metadata,
    Column("id", String(36), primary_key=True),
    Column("owner_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("term", String(200), nullable=False),
    Column("wrong", String(200), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Table(
    "meeting_templates", metadata,
    Column("id", String(36), primary_key=True),
    Column("owner_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True),
    Column("name", String(100), nullable=False),
    Column("description", Text, nullable=True),
    Column("icon", String(10), nullable=False),
    Column("sections", JSON, nullable=False),
    Column("custom_prompt", Text, nullable=True),
    Column("is_default", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Table(
    "workspace_members", metadata,
    Column("id", String(36), primary_key=True),
    Column("workspace_id", String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("role", String(20), nullable=False),
    Column("invited_at", DateTime(timezone=True), nullable=False),
    Column("joined_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
)

# Хуучин DB-д create_all() хожим нэмж чадаагүй багана (db._add_missing_columns-ийн түүх)
LEGACY_COLUMNS = [
    ("users", "name", "VARCHAR(100)"),
    ("meetings", "participants", "JSON"),
    ("meetings", "archived", "INTEGER DEFAULT 0"),
    ("meetings", "template_id", "VARCHAR(36)"),
    ("meetings", "workspace_id", "VARCHAR(36)"),
    ("meetings", "project_id", "VARCHAR(36)"),
]
