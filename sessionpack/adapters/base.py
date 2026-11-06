from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from sessionpack.model import InspectResult, Record, SourceFile


class MemoryAdapter(Protocol):
    id: str
    version: int
    source_kind: str

    def detect(self, root: Path) -> bool: ...

    def inspect(self, root: Path) -> InspectResult: ...

    def declared_paths(self, root: Path) -> list[Path]: ...

    def enumerate(self, root: Path) -> Iterator[SourceFile]: ...

    def read_file(
        self, ref: SourceFile, start_offset: int = 0, start_seq: int = 0
    ) -> Iterator[tuple[int, Record | None, str | None]]: ...

    def display_name(self, canonical_project_id: str) -> str: ...
