"""Shared schema building blocks: base model and pagination."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Server-enforced pagination cap (API conventions §7).
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class ORMModel(BaseModel):
    """Base for *Read* schemas that map from ORM instances."""

    model_config = ConfigDict(from_attributes=True)


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size


class Page[T](BaseModel):
    items: list[T]
    page: int
    size: int
    total: int
