"""KeeperHub execution adapter for blockchain transactions."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

from sweepai.core.config import KeeperHubConfig


@dataclass
class ExecutionResult:
    """Result of a KeeperHub execution."""

    execution_id: str
    status: str
    transaction_hash: str | None = None
    transaction_link: str | None = None
    error: str | None = None
    raw_response: dict[str, Any] | None = None


@dataclass
class SimulationResult:
    """Result of a dry-run simulation."""

    success: bool
    would_revert: bool = False
    from_address: str | None = None
    gas_estimate: str | None = None
    revert_reason: str | None = None
    raw_response: dict[str, Any] | None = None


class KeeperHubExecutionAdapter:
    """
    Isolates all KeeperHub-specific code.

    Uses Direct Execution API for transfers with simulation-first approach.
    """

    def __init__(self, config: KeeperHubConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.api_endpoint,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _generate_idempotency_key(
        self,
        task_id: str,
        chain_id: str,
        recipient: str,
        amount: str,
        token_address: str | None = None,
    ) -> str:
        """Generate stable idempotency key for safe retries."""
        # Normalize inputs
        normalized_chain = str(int(chain_id))  # Remove leading zeros
        normalized_recipient = recipient.lower()
        normalized_amount = self._normalize_amount(amount)
        normalized_token = token_address.lower() if token_address else ""

        # Build canonical string
        parts = [
            task_id, normalized_chain, normalized_recipient,
            normalized_amount, normalized_token,
        ]
        canonical = "|".join(parts)

        # SHA-256 hash
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_amount(amount: str) -> str:
        """Normalize amount string for idempotency."""
        from decimal import Decimal

        try:
            value = Decimal(amount.strip())
        except Exception:
            return amount.strip()

        if value == 0:
            return "0"

        # Normalize: remove leading zeros, trailing zeros after decimal
        normalized = str(value.normalize())
        # Ensure there's a digit before decimal point
        if normalized.startswith("."):
            normalized = "0" + normalized
        return normalized

    async def simulate_transfer(
        self,
        chain_id: str,
        recipient: str,
        amount: str,
        token_address: str | None = None,
    ) -> SimulationResult:
        """Simulate a transfer without broadcasting."""
        client = await self._get_client()

        payload: dict[str, Any] = {
            "chainId": chain_id,
            "recipientAddress": recipient,
            "amount": amount,
            "simulate": True,
        }
        if token_address:
            payload["tokenAddress"] = token_address

        response = await client.post("/execute/transfer", json=payload)

        if response.status_code == 200:
            data = response.json()
            return SimulationResult(
                success=data.get("success", True),
                would_revert=data.get("wouldRevert", False),
                from_address=data.get("from"),
                gas_estimate=data.get("gasEstimate"),
                revert_reason=data.get("revertReason"),
                raw_response=data,
            )
        else:
            ct = response.headers.get("content-type", "")
            data = response.json() if ct.startswith("application/json") else {}
            return SimulationResult(
                success=False,
                would_revert=data.get("wouldRevert", False),
                revert_reason=data.get("error", f"HTTP {response.status_code}"),
                raw_response=data,
            )

    async def execute_transfer(
        self,
        chain_id: str,
        recipient: str,
        amount: str,
        token_address: str | None = None,
        task_id: str = "sweep",
    ) -> ExecutionResult:
        """
        Execute a transfer via KeeperHub.

        Uses idempotency key for safe retries.
        """
        client = await self._get_client()

        idempotency_key = self._generate_idempotency_key(
            task_id=task_id,
            chain_id=chain_id,
            recipient=recipient,
            amount=amount,
            token_address=token_address,
        )

        payload: dict[str, Any] = {
            "chainId": chain_id,
            "recipientAddress": recipient,
            "amount": amount,
        }
        if token_address:
            payload["tokenAddress"] = token_address

        response = await client.post(
            "/execute/transfer",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

        if response.status_code in (200, 202):
            data = response.json()
            return ExecutionResult(
                execution_id=data.get("executionId", ""),
                status=data.get("status", "pending"),
                transaction_hash=data.get("transactionHash"),
                transaction_link=data.get("transactionLink"),
                raw_response=data,
            )
        else:
            ct = response.headers.get("content-type", "")
            data = response.json() if ct.startswith("application/json") else {}
            return ExecutionResult(
                execution_id="",
                status="failed",
                error=data.get("error", f"HTTP {response.status_code}"),
                raw_response=data,
            )

    async def get_execution_status(self, execution_id: str) -> ExecutionResult:
        """Poll execution status."""
        client = await self._get_client()

        response = await client.get(f"/execute/{execution_id}/status")

        if response.status_code == 200:
            data = response.json()
            return ExecutionResult(
                execution_id=data.get("executionId", execution_id),
                status=data.get("status", "unknown"),
                transaction_hash=data.get("transactionHash"),
                transaction_link=data.get("transactionLink"),
                error=data.get("error"),
                raw_response=data,
            )
        else:
            return ExecutionResult(
                execution_id=execution_id,
                status="unknown",
                error=f"HTTP {response.status_code}",
            )

    async def wait_for_completion(
        self,
        execution_id: str,
        max_wait: float = 120.0,
        poll_interval: float = 2.0,
    ) -> ExecutionResult:
        """Poll until execution completes or times out."""
        start = time.time()

        while time.time() - start < max_wait:
            result = await self.get_execution_status(execution_id)

            if result.status in ("completed", "failed"):
                return result

            # Honor poll interval hint if present
            if result.raw_response and "X-Poll-Interval-Hint" in str(result.raw_response):
                try:
                    hint = float(result.raw_response.get("X-Poll-Interval-Hint", poll_interval))
                    if hint > 0:
                        poll_interval = hint
                except (ValueError, TypeError):
                    pass

            import asyncio
            await asyncio.sleep(poll_interval)

        return ExecutionResult(
            execution_id=execution_id,
            status="timeout",
            error=f"Execution did not complete within {max_wait}s",
        )
