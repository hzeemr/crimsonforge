"""Per-string translation state machine.

Each localization string goes through states:
  Pending -> Translated -> Reviewed -> Approved
  (can revert at any time)
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class StringStatus(Enum):
    PENDING = "pending"
    TRANSLATED = "translated"
    REVIEWED = "reviewed"
    APPROVED = "approved"


@dataclass
class TranslationEntry:
    """State of a single translation string."""
    index: int
    key: str
    original_text: str
    translated_text: str = ""
    status: StringStatus = StringStatus.PENDING
    usage_tags: list[str] = field(default_factory=list)
    ai_provider: str = ""
    ai_model: str = ""
    ai_tokens: int = 0
    ai_cost: float = 0.0
    manually_edited: bool = False
    locked: bool = False
    notes: str = ""
    game_introduced_version: str = ""
    game_last_seen_version: str = ""
    game_last_changed_version: str = ""
    game_removed_in_version: str = ""
    game_sync_state: str = ""
    game_event_history: list[dict] = field(default_factory=list)

    def set_translated(self, text: str, provider: str = "", model: str = "",
                       tokens: int = 0, cost: float = 0.0) -> None:
        """Set translation from AI or manual input."""
        self.translated_text = text
        self.status = StringStatus.TRANSLATED
        if provider:
            self.ai_provider = provider
            self.ai_model = model
            self.ai_tokens = tokens
            self.ai_cost = cost
            self.manually_edited = False
        else:
            self.manually_edited = True

    def set_reviewed(self) -> None:
        if self.status in (StringStatus.TRANSLATED, StringStatus.APPROVED):
            self.status = StringStatus.REVIEWED

    def set_approved(self) -> None:
        if self.status in (StringStatus.TRANSLATED, StringStatus.REVIEWED):
            self.status = StringStatus.APPROVED

    def revert_to_pending(self) -> None:
        if self.locked:
            return
        self.status = StringStatus.PENDING
        self.translated_text = ""
        self.ai_provider = ""
        self.ai_model = ""
        self.ai_tokens = 0
        self.ai_cost = 0.0
        self.manually_edited = False

    def edit_translation(self, new_text: str) -> None:
        """Manually edit an existing translation."""
        if self.locked:
            return
        self.translated_text = new_text
        self.manually_edited = True
        if self.status == StringStatus.APPROVED:
            self.status = StringStatus.REVIEWED

    def record_game_event(self, version: str, kind: str, details: str = "") -> None:
        """Record a game-text lifecycle event for version-aware filtering."""
        if not version or not kind:
            return

        event = {"version": version, "kind": kind}
        if details:
            event["details"] = details

        if self.game_event_history:
            last = self.game_event_history[-1]
            if (
                last.get("version") == version
                and last.get("kind") == kind
                and last.get("details", "") == details
            ):
                self.game_sync_state = kind
                return

        self.game_event_history.append(event)
        self.game_sync_state = kind

        if kind in ("baseline", "added") and not self.game_introduced_version:
            self.game_introduced_version = version
        if kind == "changed":
            self.game_last_changed_version = version
            self.game_removed_in_version = ""
        elif kind == "removed":
            self.game_removed_in_version = version
        elif kind in ("baseline", "added"):
            self.game_removed_in_version = ""

    def clear_game_sync_state(self) -> None:
        self.game_sync_state = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "key": self.key,
            "original_text": self.original_text,
            "translated_text": self.translated_text,
            "status": self.status.value,
            "usage_tags": list(self.usage_tags),
            "ai_provider": self.ai_provider,
            "ai_model": self.ai_model,
            "ai_tokens": self.ai_tokens,
            "ai_cost": self.ai_cost,
            "manually_edited": self.manually_edited,
            "locked": self.locked,
            "notes": self.notes,
            "game_introduced_version": self.game_introduced_version,
            "game_last_seen_version": self.game_last_seen_version,
            "game_last_changed_version": self.game_last_changed_version,
            "game_removed_in_version": self.game_removed_in_version,
            "game_sync_state": self.game_sync_state,
            "game_event_history": list(self.game_event_history),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranslationEntry":
        entry = cls(
            index=data["index"],
            key=data["key"],
            original_text=data["original_text"],
            translated_text=data.get("translated_text", ""),
            usage_tags=list(data.get("usage_tags", [])),
            ai_provider=data.get("ai_provider", ""),
            ai_model=data.get("ai_model", ""),
            ai_tokens=data.get("ai_tokens", 0),
            ai_cost=data.get("ai_cost", 0.0),
            manually_edited=data.get("manually_edited", False),
            locked=data.get("locked", False),
            notes=data.get("notes", ""),
            game_introduced_version=data.get("game_introduced_version", ""),
            game_last_seen_version=data.get("game_last_seen_version", ""),
            game_last_changed_version=data.get("game_last_changed_version", ""),
            game_removed_in_version=data.get("game_removed_in_version", ""),
            game_sync_state=data.get("game_sync_state", ""),
            game_event_history=list(data.get("game_event_history", [])),
        )
        status_str = data.get("status", "pending")
        entry.status = StringStatus(status_str)
        return entry
