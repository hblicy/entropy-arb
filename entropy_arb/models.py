"""Normalized domain values shared across venue adapters and the engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrderResult:
    status: str
    filled_base: float = 0.0
    avg_px: Optional[float] = None
    err: Optional[str] = None
    unresolved: bool = False

    def __post_init__(self) -> None:
        if not self.status:
            raise ValueError("status must not be empty")
        if self.filled_base < 0:
            raise ValueError("filled_base must be >= 0")
        if self.avg_px is not None and self.avg_px <= 0:
            raise ValueError("avg_px must be > 0 when present")

    @classmethod
    def send_failed(cls, err: str) -> "OrderResult":
        if not err:
            raise ValueError("send failure must include an error")
        return cls(status="send-failed", err=err)

    @classmethod
    def unknown(cls, status: str = "timeout") -> "OrderResult":
        return cls(status=status, unresolved=True)

    @property
    def rate_limited(self) -> bool:
        return bool(self.err and self.err.startswith("RATE_LIMITED"))
