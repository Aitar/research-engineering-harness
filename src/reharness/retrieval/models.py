from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SearchQuery:
    text: str = ""
    entity_types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    evidence_types: tuple[str, ...] = ()
    task_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 20
    strategy: str = "hybrid"
    graph_depth: int = 1
    include_superseded: bool = True


@dataclass
class SearchResult:
    entity_type: str
    entity_id: str
    chunk_type: str
    title: str
    excerpt: str
    status: str | None
    authority_level: int
    integrity_status: str | None
    score: float
    match_sources: list[str] = field(default_factory=list)
    source_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return self.entity_type, self.entity_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
