from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterable


class Substrate(ABC):
    @abstractmethod
    def read(self, path: str) -> str | None: ...

    @abstractmethod
    def write(self, path: str, content: str) -> None: ...

    @abstractmethod
    def append(self, path: str, content: str) -> None: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str) -> Iterable[str]: ...


class LocalSubstrate(Substrate):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full(self, path: str) -> Path:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def read(self, path: str) -> str | None:
        full = self.root / path
        if not full.exists():
            return None
        return full.read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        self._full(path).write_text(content, encoding="utf-8")

    def append(self, path: str, content: str) -> None:
        full = self._full(path)
        with full.open("a", encoding="utf-8") as f:
            f.write(content)

    def delete(self, path: str) -> None:
        full = self.root / path
        if full.exists():
            full.unlink()

    def list(self, prefix: str) -> Iterable[str]:
        base = self.root / prefix
        if not base.exists():
            return []
        return [
            str(p.relative_to(self.root)).replace(os.sep, "/")
            for p in base.rglob("*.md")
            if p.is_file()
        ]


