"""Configuration management for SweepAI."""

from __future__ import annotations

import os
import tomllib as tomli
from dataclasses import dataclass
from pathlib import Path

from sweepai.core.models import TreasuryPolicy, WalletConfig


@dataclass
class KeeperHubConfig:
    """KeeperHub connection configuration."""

    api_key: str
    mcp_endpoint: str = "https://app.keeperhub.com/mcp"
    api_endpoint: str = "https://app.keeperhub.com/api"

    @classmethod
    def from_env(cls) -> KeeperHubConfig:
        """Load from environment variables."""
        api_key = os.environ.get("KEEPERHUB_API_KEY", "")
        if not api_key:
            raise ValueError("KEEPERHUB_API_KEY environment variable is required")
        return cls(api_key=api_key)


@dataclass
class AppConfig:
    """Main application configuration."""

    wallet: WalletConfig
    policy: TreasuryPolicy
    keeperhub: KeeperHubConfig
    db_path: str = "data/sweepai.db"


def load_config(config_path: str | Path = "config/config.toml") -> AppConfig:
    """Load configuration from TOML file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "rb") as f:
        raw = tomli.load(f)

    # Parse wallet config
    treasury = raw.get("treasury", {})
    wallet = WalletConfig(
        address=treasury["source_address"],
        chain_id=str(treasury["chain_id"]),
        label="source",
        token_address=treasury.get("token_address"),
    )

    # Parse policy config
    policy_raw = raw.get("policy", {})
    policy = TreasuryPolicy(
        sweep_threshold=str(policy_raw["sweep_threshold"]),
        min_sweep_amount=str(policy_raw["min_sweep_amount"]),
        max_sweep_amount=str(policy_raw["max_sweep_amount"]),
        gas_reserve=str(policy_raw["gas_reserve"]),
        cooldown_seconds=int(policy_raw.get("cooldown_seconds", 3600)),
        allowed_destinations=tuple(policy_raw["allowed_destinations"]),
        chain_ids=tuple(str(c) for c in policy_raw["chain_ids"]),
    )

    # Parse keeperhub config
    kh_raw = raw.get("keeperhub", {})
    keeperhub = KeeperHubConfig(
        api_key=os.environ.get("KEEPERHUB_API_KEY", ""),
        mcp_endpoint=kh_raw.get("mcp_endpoint", "https://app.keeperhub.com/mcp"),
        api_endpoint=kh_raw.get("api_endpoint", "https://app.keeperhub.com/api"),
    )

    # Database path
    db_path = raw.get("database", {}).get("path", "data/sweepai.db")

    return AppConfig(
        wallet=wallet,
        policy=policy,
        keeperhub=keeperhub,
        db_path=db_path,
    )
