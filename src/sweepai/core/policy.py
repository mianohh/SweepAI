"""Deterministic policy engine for sweep validation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sweepai.core.models import SweepProposal, TreasuryPolicy


class PolicyError(Exception):
    """Raised when a proposal violates policy rules."""

    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        self.message = message
        super().__init__(f"Policy violation [{rule}]: {message}")


class PolicyEngine:
    """Deterministic validation of sweep proposals against treasury policy."""

    def __init__(self, policy: TreasuryPolicy) -> None:
        self.policy = policy

    def validate_amount(self, amount: str) -> Decimal:
        """Parse and validate amount as decimal string."""
        try:
            value = Decimal(amount)
        except InvalidOperation:
            raise PolicyError("invalid_amount", f"Cannot parse amount: {amount}")

        if value <= 0:
            raise PolicyError("invalid_amount", f"Amount must be positive: {amount}")

        return value

    def validate_destination(self, address: str) -> None:
        """Check destination is in allowlist."""
        if not self.policy.is_destination_allowed(address):
            raise PolicyError(
                "unapproved_destination",
                f"Destination {address} is not in allowed destinations",
            )

    def validate_chain(self, chain_id: str) -> None:
        """Check chain is supported."""
        if not self.policy.is_chain_allowed(chain_id):
            raise PolicyError(
                "unsupported_chain",
                f"Chain {chain_id} is not in allowed chains",
            )

    def validate_sweep_bounds(self, amount: Decimal) -> None:
        """Check amount is within min/max bounds."""
        min_amount = Decimal(self.policy.min_sweep_amount)
        max_amount = Decimal(self.policy.max_sweep_amount)

        if amount < min_amount:
            raise PolicyError(
                "below_minimum",
                f"Sweep amount {amount} is below minimum {min_amount}",
            )

        if amount > max_amount:
            raise PolicyError(
                "above_maximum",
                f"Sweep amount {amount} exceeds maximum {max_amount}",
            )

    def validate_gas_reserve(self, balance: str, sweep_amount: str) -> None:
        """Ensure sufficient gas reserve remains after sweep."""
        balance_decimal = self.validate_amount(balance)
        sweep_decimal = self.validate_amount(sweep_amount)
        reserve = Decimal(self.policy.gas_reserve)

        remaining = balance_decimal - sweep_decimal
        if remaining < reserve:
            raise PolicyError(
                "insufficient_gas_reserve",
                f"Remaining {remaining} would be below gas reserve {reserve}",
            )

    def validate_cooldown(
        self, last_sweep_timestamp: float | None, current_timestamp: float
    ) -> None:
        """Check cooldown period has elapsed."""
        if last_sweep_timestamp is None:
            return

        elapsed = current_timestamp - last_sweep_timestamp
        if elapsed < self.policy.cooldown_seconds:
            remaining = self.policy.cooldown_seconds - elapsed
            raise PolicyError(
                "cooldown_active",
                f"Cooldown active, {remaining:.0f} seconds remaining",
            )

    def validate_proposal(
        self,
        proposal: SweepProposal,
        current_balance: str,
        last_sweep_timestamp: float | None = None,
        current_timestamp: float | None = None,
    ) -> list[str]:
        """
        Run all policy checks on a proposal.

        Returns list of warnings (non-fatal).
        Raises PolicyError on fatal errors.
        """
        import time

        if current_timestamp is None:
            current_timestamp = time.time()

        warnings: list[str] = []

        # Chain validation
        self.validate_chain(proposal.chain_id)

        # Destination validation
        self.validate_destination(proposal.destination_address)

        # Amount parsing and validation
        amount = self.validate_amount(proposal.amount)

        # Sweep bounds
        self.validate_sweep_bounds(amount)

        # Gas reserve check
        self.validate_gas_reserve(current_balance, proposal.amount)

        # Cooldown check
        self.validate_cooldown(last_sweep_timestamp, current_timestamp)

        return warnings

    def calculate_sweep_amount(self, balance: str) -> str | None:
        """
        Calculate optimal sweep amount based on policy.

        Returns None if no sweep is needed.
        """
        balance_decimal = self.validate_amount(balance)
        threshold = Decimal(self.policy.sweep_threshold)
        min_sweep = Decimal(self.policy.min_sweep_amount)
        max_sweep = Decimal(self.policy.max_sweep_amount)
        reserve = Decimal(self.policy.gas_reserve)

        # Check if balance exceeds threshold
        if balance_decimal <= threshold:
            return None

        # Calculate excess above threshold
        excess = balance_decimal - threshold

        # Apply gas reserve
        available = balance_decimal - reserve
        if available <= 0:
            return None

        # Sweep the excess, but cap at max
        sweep_amount = min(excess, available, max_sweep)

        # Ensure minimum
        if sweep_amount < min_sweep:
            return None

        # Normalize: remove trailing zeros
        return str(sweep_amount.normalize())
