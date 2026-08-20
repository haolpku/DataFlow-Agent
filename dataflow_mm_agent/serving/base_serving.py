"""Common model-serving interface consumed by Agent-MM runtimes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..contracts import Message


class ModelServing(ABC):
    @abstractmethod
    def generate_messages(
        self,
        conversations: Sequence[Sequence[Message]],
    ) -> list[str]:
        raise NotImplementedError

    def generate(self, messages: Sequence[Message]) -> str:
        results = self.generate_messages([messages])
        if len(results) != 1:
            raise RuntimeError("serving must return exactly one result per request")
        return results[0]

    def health_check(self) -> bool:
        return True

    def cleanup(self) -> None:
        return None


__all__ = ["ModelServing"]
