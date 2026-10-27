"""Vision call budget: repair work must never stall a batch."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VisionBudget:
    """Per-page and per-batch limits for repair calls."""
    per_page: int = 2
    per_batch: int = 8
    used: int = 0
    per_page_used: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)

    def allow(self, page) -> tuple:
        page_count = self.per_page_used.get(page, 0)
        if page_count >= self.per_page:
            return False, "PER_PAGE_BUDGET"
        if self.used >= self.per_batch:
            return False, "BATCH_BUDGET"
        return True, None

    def consume(self, page, *, status: str = "used") -> None:
        self.used += 1
        self.per_page_used[page] = self.per_page_used.get(page, 0) + 1
        self.skipped.append({"page": page, "status": status})

    def skip(self, page, reason: str) -> None:
        self.skipped.append({"page": page, "status": "skipped", "reason": reason})

    def summary(self) -> dict:
        return {"used": self.used, "per_page": dict(self.per_page_used),
                "skipped": list(self.skipped), "limits": {"per_page": self.per_page,
                                                          "per_batch": self.per_batch}}
