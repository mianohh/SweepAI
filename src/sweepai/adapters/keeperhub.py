"""KeeperHub execution adapter for blockchain transactions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from sweepai.core.config import KeeperHubConfig

logger = logging.getLogger("sweepai.keeperhub")


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
    Includes retry logic with exponential backoff for transient failures.
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

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make HTTP request with retry logic for transient failures."""
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                client = await self._get_client()
                response = await client.request(method, url, **kwargs)

                # Retry on rate limit or server errors
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                    logger.warning(
                        "Rate limited, waiting %.1fs (attempt %d/%d)",
                        retry_after, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if response.status_code >= 500:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "Server error %d, waiting %ds (attempt %d/%d)",
                        response.status_code, wait_time, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                return response

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                wait_time = 2 ** attempt
                logger.warning(
                    "Request failed: %s, waiting %ds (attempt %d/%d)",
                    e, wait_time, attempt + 1, max_retries,
                )
                await asyncio.sleep(wait_time)

        if last_error:
            raise last_error
        raise RuntimeError(f"Request failed after {max_retries} attempts")

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
        payload: dict[str, Any] = {
            "chainId": chain_id,
            "recipientAddress": recipient,
            "amount": amount,
            "simulate": True,
        }
        if token_address:
            payload["tokenAddress"] = token_address

        logger.info(
            "Simulating transfer: chain=%s, recipient=%s, amount=%s",
            chain_id, recipient[:10] + "...", amount,
        )

        response = await self._request_with_retry(
            "POST", "/execute/transfer", json=payload,
        )

        if response.status_code == 200:
            data = response.json()
            result = SimulationResult(
                success=data.get("success", True),
                would_revert=data.get("wouldRevert", False),
                from_address=data.get("from"),
                gas_estimate=data.get("gasEstimate"),
                revert_reason=data.get("revertReason"),
                raw_response=data,
            )
            logger.info(
                "Simulation result: success=%s, would_revert=%s",
                result.success, result.would_revert,
            )
            return result
        else:
            ct = response.headers.get("content-type", "")
            data = response.json() if ct.startswith("application/json") else {}
            logger.error(
                "Simulation failed: status=%d, error=%s",
                response.status_code, data.get("error"),
            )
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

        logger.info(
            "Executing transfer: chain=%s, recipient=%s, amount=%s",
            chain_id, recipient[:10] + "...", amount,
        )

        response = await self._request_with_retry(
            "POST",
            "/execute/transfer",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

        if response.status_code in (200, 202):
            data = response.json()
            result = ExecutionResult(
                execution_id=data.get("executionId", ""),
                status=data.get("status", "pending"),
                transaction_hash=data.get("transactionHash"),
                transaction_link=data.get("transactionLink"),
                raw_response=data,
            )
            logger.info(
                "Execution submitted: id=%s, status=%s, tx=%s",
                result.execution_id, result.status, result.transaction_hash or "pending",
            )
            return result
        else:
            ct = response.headers.get("content-type", "")
            data = response.json() if ct.startswith("application/json") else {}
            error_msg = data.get("error", f"HTTP {response.status_code}")
            logger.error("Execution failed: %s", error_msg)
            return ExecutionResult(
                execution_id="",
                status="failed",
                error=error_msg,
                raw_response=data,
            )

    async def get_execution_status(self, execution_id: str) -> ExecutionResult:
        """Poll execution status."""
        response = await self._request_with_retry(
            "GET", f"/execute/{execution_id}/status",
        )

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

            await asyncio.sleep(poll_interval)

        return ExecutionResult(
            execution_id=execution_id,
            status="timeout",
            error=f"Execution did not complete within {max_wait}s",
        )
