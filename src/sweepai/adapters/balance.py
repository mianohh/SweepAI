"""Balance reader adapter for querying wallet balances."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


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

    # ERC-20 balanceOf ABI (minimal)
    BALANCE_OF_ABI = (
        '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],'
        '"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],'
        '"type":"function"}]'
    )

    # ERC-20 decimals ABI
    DECIMALS_ABI = (
        '[{"constant":true,"inputs":[],"name":"decimals",'
        '"outputs":[{"name":"","type":"uint8"}],"type":"function"}]'
    )

    # ERC-20 symbol ABI
    SYMBOL_ABI = (
        '[{"constant":true,"inputs":[],"name":"symbol",'
        '"outputs":[{"name":"","type":"string"}],"type":"function"}]'
    )

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
    ) -> dict:
        """Make an RPC call."""
        client = await self._get_client()
        rpc_url = self._get_rpc_url(chain_id)

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }

        response = await client.post(rpc_url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")

        return data.get("result", {})

    async def read_native_balance(self, address: str, chain_id: str) -> BalanceInfo:
        """Read native token balance (ETH, MATIC, etc.)."""
        result = await self._rpc_call(chain_id, "eth_getBalance", [address, "latest"])

        # Convert hex wei to decimal ether
        wei = int(result, 16)
        ether = wei / 10**18

        return BalanceInfo(
            address=address,
            chain_id=chain_id,
            balance=str(ether),
        )

    async def _erc20_call(
        self, chain_id: str, contract: str, abi: str, args: str
    ) -> str:
        """Make an eth_call to an ERC-20 contract."""
        client = await self._get_client()
        rpc_url = self._get_rpc_url(chain_id)

        # Encode function call (simplified - uses eth_call with data)
        # In production, use web3.py or eth-abi for proper encoding

        # For now, use a direct RPC call with encoded data
        # This is a simplified version - full implementation would use eth-abi
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [
                {
                    "to": contract,
                    "data": args,  # Pre-encoded function call
                },
                "latest",
            ],
        }

        response = await client.post(rpc_url, json=payload)
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise RuntimeError(f"ERC-20 call error: {data['error']}")

        return data.get("result", "0x0")

    async def read_token_balance(
        self, address: str, chain_id: str, token_address: str
    ) -> BalanceInfo:
        """Read ERC-20 token balance."""
        # Simplified: In production, properly encode balanceOf call
        # For MVP, we'll use the RPC directly with pre-encoded data
        # balanceOf(address) selector: 0x70a08231
        padded_address = address.lower().replace("0x", "").zfill(64)
        call_data = f"0x70a08231{padded_address}"

        result = await self._erc20_call(chain_id, token_address, self.BALANCE_OF_ABI, call_data)

        # Parse uint256 result
        balance_raw = int(result, 16)

        # Get decimals (default 18)
        decimals = 18

        # Convert to human-readable
        balance = balance_raw / 10**decimals

        return BalanceInfo(
            address=address,
            chain_id=chain_id,
            balance=str(balance),
            token_address=token_address,
            token_decimals=decimals,
        )

    async def read_balance(
        self, address: str, chain_id: str, token_address: str | None = None
    ) -> BalanceInfo:
        """Read balance for native or ERC-20 token."""
        if token_address:
            return await self.read_token_balance(address, chain_id, token_address)
        return await self.read_native_balance(address, chain_id)
