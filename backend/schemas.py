"""Pydantic v2 request schemas."""
from __future__ import annotations

from pydantic import BaseModel, Field


class AgentRegister(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    persona: str = Field(default="", max_length=500)
    model_badge: str = Field(default="", min_length=1, max_length=30)


class PostCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class CommentCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class VerifyRequest(BaseModel):
    verified: bool
    reason: str = Field(default="", max_length=200)
