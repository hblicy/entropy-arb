"""Normalized domain values shared across venue adapters and the engine."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrderResult:
    status: str
    filled_base: float = 0.0
    avg_px: Optional[float] = None
    err: Optional[str] = None
    unresolved: bool = False
    order_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.status:
            raise ValueError("status must not be empty")
        if not math.isfinite(self.filled_base) or self.filled_base < 0:
            raise ValueError("filled_base must be finite and >= 0")
        if self.filled_base > 0 and self.avg_px is None:
            raise ValueError("avg_px is required when filled_base is positive")
        if (self.avg_px is not None
                and (not math.isfinite(self.avg_px) or self.avg_px <= 0)):
            raise ValueError("avg_px must be finite and > 0 when present")

    @classmethod
    def send_failed(cls, err: str) -> "OrderResult":
        if not err:
            raise ValueError("send failure must include an error")
        return cls(status="send-failed", err=err)

    @classmethod
    def unknown(cls, status: str = "timeout",
                err: Optional[str] = None,
                order_ref: Optional[str] = None) -> "OrderResult":
        return cls(
            status=status, err=err, unresolved=True, order_ref=order_ref)

    @property
    def rate_limited(self) -> bool:
        return bool(self.err and self.err.startswith("RATE_LIMITED"))
