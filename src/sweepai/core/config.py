"""Configuration management for SweepAI."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sweepai.core.models import TreasuryPolicy, WalletConfig


@dataclass
class KeeperHubConfig:
    """KeeperHub connection configuration."""

    api_key: str
    mcp_endpoint: str = "https://app.keeperhub.com/mcp"
    api_endpoint: str = "https://app.keeperhub.com/api"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("KeeperHub API key is required")
        if not self.api_key.startswith("kh_"):
            raise ValueError("KeeperHub API key must start with 'kh_'")

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


def _validate_numeric_string(value: str, field_name: str) -> Decimal:
    """Validate and parse a numeric string as Decimal."""
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field_name} must be a valid number: {value}")
    if decimal_value <= 0:
        raise ValueError(f"{field_name} must be positive: {value}")
    return decimal_value


def load_config(config_path: str | Path = "config/config.toml") -> AppConfig:
    """Load configuration from TOML file with validation."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    # Parse and validate wallet config
    treasury = raw.get("treasury", {})
    if "source_address" not in treasury:
        raise ValueError("treasury.source_address is required")
    if "chain_id" not in treasury:
        raise ValueError("treasury.chain_id is required")

    wallet = WalletConfig(
        address=treasury["source_address"],
        chain_id=str(treasury["chain_id"]),
        label="source",
        token_address=treasury.get("token_address"),
    )

    # Parse and validate policy config
    policy_raw = raw.get("policy", {})
    required_policy_fields = [
        "sweep_threshold",
        "min_sweep_amount",
        "max_sweep_amount",
        "gas_reserve",
        "allowed_destinations",
        "chain_ids",
    ]
    for field_name in required_policy_fields:
        if field_name not in policy_raw:
            raise ValueError(f"policy.{field_name} is required")

    # Validate numeric fields
    _validate_numeric_string(str(policy_raw["sweep_threshold"]), "sweep_threshold")
    _validate_numeric_string(str(policy_raw["min_sweep_amount"]), "min_sweep_amount")
    _validate_numeric_string(str(policy_raw["max_sweep_amount"]), "max_sweep_amount")
    _validate_numeric_string(str(policy_raw["gas_reserve"]), "gas_reserve")

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
    api_key = os.environ.get("KEEPERHUB_API_KEY", "")
    keeperhub = KeeperHubConfig(
        api_key=api_key,
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
