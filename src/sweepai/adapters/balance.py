"""Balance reader adapter for querying wallet balances."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("sweepai.balance")


@dataclass
class BalanceInfo:
    """Balance information for a wallet."""

    address: str
    chain_id: str
    balance: str  # Decimal string
    token_address: str | None = None
    token_symbol: str | None = None
    token_decimals: int | None = None


class BalanceReader:
    """
    Read wallet balances from blockchain.

    Uses public RPC endpoints for balance queries.
    """

    # Common RPC endpoints by chain ID
    RPC_ENDPOINTS: dict[str, str] = {
        "1": "https://eth.llamarpc.com",
        "11155111": "https://rpc.sepolia.org",
        "8453": "https://mainnet.base.org",
        "84532": "https://sepolia.base.org",
        "42161": "https://arb1.arbitrum.io/rpc",
        "137": "https://polygon-rpc.com",
    }

    # Known token decimals
    KNOWN_TOKEN_DECIMALS: dict[str, int] = {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,  # USDC on Ethereum
        "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,  # USDT on Ethereum
        "0x4c9edd5852c9518f94211d949e4a20dffc3c1e47": 6,  # USDC on Base
    }

    # Default decimals for native tokens
    DEFAULT_DECIMALS = 18

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _get_rpc_url(self, chain_id: str) -> str:
        """Get RPC URL for chain, checking env override first."""
        env_key = f"RPC_URL_{chain_id}"
        env_url = os.environ.get(env_key)
        if env_url:
            return env_url
        return self.RPC_ENDPOINTS.get(chain_id, f"https://rpc.ankr.com/eth_{chain_id}")

    async def _rpc_call(
        self, chain_id: str, method: str, params: list[str]
    ) -> Any:
        """Make an RPC call with retry logic."""
        client = await self._get_client()
        rpc_url = self._get_rpc_url(chain_id)

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }

        for attempt in range(3):
            try:
                response = await client.post(rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()

                if "error" in data:
                    raise RuntimeError(f"RPC error: {data['error']}")

                return data.get("result", {})
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                if attempt == 2:
                    raise
                logger.warning("RPC call failed (attempt %d/3): %s", attempt + 1, e)
                import asyncio
                await asyncio.sleep(1 * (attempt + 1))

        return {}

    async def _erc20_call(
        self, chain_id: str, contract: str, call_data: str
    ) -> str:
        """Make an eth_call to an ERC-20 contract."""
        client = await self._get_client()
        rpc_url = self._get_rpc_url(chain_id)

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {
                    "to": contract,
                    "data": call_data,
                },
                "latest",
            ],
        }

        response = await client.post(rpc_url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise RuntimeError(f"ERC-20 call error: {data['error']}")

        return str(data.get("result", "0x0"))

    async def _get_token_decimals(self, chain_id: str, token_address: str) -> int:
        """Get decimals for an ERC-20 token."""
        # Check known tokens first
        known_key = f"{chain_id}:{token_address.lower()}"
        if known_key in self.KNOWN_TOKEN_DECIMALS:
            return self.KNOWN_TOKEN_DECIMALS[known_key]

        # decimals() selector: 0x313ce567
        try:
            result = await self._erc20_call(chain_id, token_address, "0x313ce567")
            if result and result != "0x":
                return int(result, 16)
        except Exception as e:
            logger.warning("Failed to get decimals for %s: %s", token_address, e)

        return self.DEFAULT_DECIMALS

    async def _get_token_symbol(self, chain_id: str, token_address: str) -> str | None:
        """Get symbol for an ERC-20 token."""
        # symbol() selector: 0x95d89e4e
        try:
            result = await self._erc20_call(chain_id, token_address, "0x95d89e4e")
            if result and result != "0x":
                # Decode ABI-encoded string
                try:
                    data = bytes.fromhex(result[2:] if result.startswith("0x") else result)
                    # Simple decode: skip first 32 bytes (offset), read length, then data
                    if len(data) >= 64:
                        length = int.from_bytes(data[32:64], "big")
                        symbol_bytes = data[64:64 + length]
                        return symbol_bytes.decode("utf-8")
                except Exception:
                    pass
        except Exception:
            pass

        return None

    async def read_native_balance(self, address: str, chain_id: str) -> BalanceInfo:
        """Read native token balance (ETH, MATIC, etc.)."""
        result: str = await self._rpc_call(chain_id, "eth_getBalance", [address, "latest"])

        # Convert hex wei to decimal ether
        wei = int(result, 16)
        ether = wei / 10**self.DEFAULT_DECIMALS

        return BalanceInfo(
            address=address,
            chain_id=chain_id,
            balance=str(ether),
        )

    async def read_token_balance(
        self, address: str, chain_id: str, token_address: str
    ) -> BalanceInfo:
        """Read ERC-20 token balance with dynamic decimals."""
        # balanceOf(address) selector: 0x70a08231
        padded_address = address.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{padded_address}"

        result: str = await self._erc20_call(chain_id, token_address, call_data)

        # Parse uint256 result
        balance_raw = int(result, 16)

        # Get decimals dynamically
        decimals = await self._get_token_decimals(chain_id, token_address)

        # Get symbol
        symbol = await self._get_token_symbol(chain_id, token_address)

        # Convert to human-readable
        balance = balance_raw / 10**decimals

        return BalanceInfo(
            address=address,
            chain_id=chain_id,
            balance=str(balance),
            token_address=token_address,
            token_symbol=symbol,
            token_decimals=decimals,
        )

    async def read_balance(
        self, address: str, chain_id: str, token_address: str | None = None
    ) -> BalanceInfo:
        """Read balance for native or ERC-20 token."""
        if token_address:
            return await self.read_token_balance(address, chain_id, token_address)
        return await self.read_native_balance(address, chain_id)
