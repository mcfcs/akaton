from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from akaton.domain.models import CandidateSeed


class SearchRequest(BaseModel):
    query: str
    country: str = "PH"
    search_lang: str = "en"
    freshness: str | None = None
    count: int = Field(default=20, ge=1, le=20)
    offset: int = Field(default=0, ge=0)


class SearchPage(BaseModel):
    results: list[CandidateSeed]
    request_count: int = 1


class SearchProvider(Protocol):
    name: str

    async def search(self, request: SearchRequest) -> SearchPage: ...


class SourceAdapter(Protocol):
    name: str

    async def discover(
        self, since: datetime | None = None, cursor: str | None = None
    ) -> list[CandidateSeed]: ...
