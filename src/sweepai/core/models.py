"""Core data models for SweepAI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4


class SweepState(StrEnum):
    """State machine states for sweep operations."""

    IDLE = "idle"
    OBSERVING = "observing"
    EVALUATION = "evaluation"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass(frozen=True)
class WalletConfig:
    """Configuration for a monitored wallet."""

    address: str
    chain_id: str
    label: str
    token_address: str | None = None

    def __post_init__(self) -> None:
        if not self.address.startswith("0x") or len(self.address) != 42:
            raise ValueError(f"Invalid address format: {self.address}")
        if not self.chain_id.isdigit():
            raise ValueError(f"Chain ID must be numeric: {self.chain_id}")


@dataclass(frozen=True)
class TreasuryPolicy:
    """Deterministic policy rules for sweep operations."""

    sweep_threshold: str
    min_sweep_amount: str
    max_sweep_amount: str
    gas_reserve: str
    cooldown_seconds: int
    allowed_destinations: tuple[str, ...]
    chain_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        from decimal import Decimal

        threshold = Decimal(self.sweep_threshold)
        min_sweep = Decimal(self.min_sweep_amount)
        max_sweep = Decimal(self.max_sweep_amount)
        reserve = Decimal(self.gas_reserve)

        if min_sweep >= max_sweep:
            raise ValueError("min_sweep_amount must be less than max_sweep_amount")
        if reserve >= threshold:
            raise ValueError("gas_reserve must be less than sweep_threshold")
        if not self.allowed_destinations:
            raise ValueError("At least one allowed destination is required")
        if not self.chain_ids:
            raise ValueError("At least one chain ID is required")

    def is_destination_allowed(self, address: str) -> bool:
        """Check if destination address is in allowlist."""
        return address.lower() in {d.lower() for d in self.allowed_destinations}

    def is_chain_allowed(self, chain_id: str) -> bool:
        """Check if chain ID is supported."""
        return chain_id in self.chain_ids


@dataclass
class SweepProposal:
    """A proposed sweep transaction awaiting approval."""

    id: str = field(default_factory=lambda: str(uuid4()))
    source_address: str = ""
    destination_address: str = ""
    amount: str = "0"
    chain_id: str = ""
    token_address: str | None = None
    reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: SweepState = SweepState.PROPOSED

    def approve(self) -> None:
        """Approve this proposal for execution."""
        if self.status != SweepState.PROPOSED:
            raise ValueError(f"Cannot approve proposal in state: {self.status}")
        self.status = SweepState.APPROVED

    def reject(self, reason: str = "") -> None:
        """Reject this proposal."""
        if self.status != SweepState.PROPOSED:
            raise ValueError(f"Cannot reject proposal in state: {self.status}")
        self.status = SweepState.REJECTED
        if reason:
            self.reason = f"{self.reason} | Rejected: {reason}"


@dataclass
class AuditRecord:
    """Record of a sweep execution for audit trail."""

    id: str = field(default_factory=lambda: str(uuid4()))
    proposal_id: str = ""
    execution_id: str | None = None
    transaction_hash: str | None = None
    status: SweepState = SweepState.IDLE
    gas_used: str | None = None
    error: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str] = field(default_factory=dict)
