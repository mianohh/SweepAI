# SweepAI

Autonomous Treasury Management Agent for Hot-to-Vault Sweeps on EVM-compatible blockchains.

SweepAI monitors a hot wallet, determines when the balance exceeds a configurable threshold, and proposes sweep transactions that move excess funds to a designated vault — all executed via [KeeperHub](https://app.keeperhub.com).

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐     ┌─────────────┐
│  load_policy │ ──► │ read_balances │ ──► │  analyze (sweep) │ ──► │   validate   │
└─────────────┘     └──────────────┘     └──────────────────┘     └──────┬──────┘
                                                                        │
                                                          ┌─────────────┴─────────────┐
                                                          │                           │
                                                     ┌────▼────┐               ┌───────▼──────┐
                                                     │ execute  │               │    audit     │
                                                     │(KeeperHub)│               │  (database)  │
                                                     └────┬────┘               └───────┬──────┘
                                                          │                           │
                                                          └─────────────┬─────────────┘
                                                                        │
                                                                       END
```

The workflow runs as a **LangGraph state machine** with deterministic policy validation at every step. Sweep decisions are made by a policy engine that evaluates balance thresholds, gas reserves, and configurable sweep parameters.

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd SweepAI

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install the package
pip install -e ".[dev]"
```

## Configuration

### 1. Environment Variables

Copy the example and fill in your values:

```bash
cp .env.example .env
```

```env
# KeeperHub API key (required for execution)
KEEPERHUB_API_KEY=kh_your_key_here

# Custom RPC endpoints (optional, overrides defaults per chain)
RPC_URL_11155111=https://ethereum-sepolia-rpc.publicnode.com
```

### 2. Application Config

Copy the example config and customize:

```bash
cp config/config.example.toml config/config.toml
```

```toml
[treasury]
# Hot wallet to monitor and sweep
source_address = "0xYOUR_HOT_WALLET_ADDRESS"
chain_id = "11155111"  # Sepolia testnet

[policy]
sweep_threshold = "0.1"      # Sweep when balance exceeds this
min_sweep_amount = "0.01"    # Minimum per sweep
max_sweep_amount = "0.1"     # Maximum per sweep
gas_reserve = "0.01"         # Retained for gas fees
cooldown_seconds = 3600      # Min seconds between sweeps

allowed_destinations = [
    "0xYOUR_VAULT_ADDRESS"
]
chain_ids = ["11155111"]

[keeperhub]
mcp_endpoint = "https://app.keeperhub.com/mcp"
api_endpoint = "https://app.keeperhub.com/api"

[database]
path = "data/sweepai.db"
```

## Usage

### CLI Commands

```bash
# Verify configuration and connections
sweepai doctor

# Read current wallet balance
sweepai observe

# Run sweep analysis
sweepai evaluate

# Generate and store a sweep proposal
sweepai propose

# Approve a pending proposal
sweepai approve <proposal_id>

# Execute an approved proposal via KeeperHub
sweepai execute <proposal_id>

# Check execution status
sweepai status <execution_id>

# View audit trail
sweepai audit

# Emergency pause all operations
sweepai pause

# Resume operations
sweepai unpause
```

### Example Workflow

```bash
# 1. Check everything is configured
sweepai doctor

# 2. Observe current balance
sweepai observe

# 3. Evaluate whether to sweep
sweepai evaluate

# 4. Create a proposal
sweepai propose

# 5. Approve it
sweepai approve <proposal_id>

# 6. Execute via KeeperHub
sweepai execute <proposal_id>
```

## How KeeperHub Integration Works

SweepAI uses KeeperHub's **Direct Execution API** with a simulation-first approach:

| Step | Endpoint | Description |
|------|----------|-------------|
| Simulate | `POST /execute/transfer` (simulate=true) | Dry-run without broadcasting |
| Execute | `POST /execute/transfer` (with Idempotency-Key) | Actual on-chain transfer |
| Status | `GET /execute/{id}/status` | Poll until completion |

The system always simulates before executing. If simulation fails or would revert, execution is aborted — preventing failed transactions and wasted gas.

Idempotency keys are generated using SHA-256 hashing of normalized inputs (task_id, chain_id, recipient, amount, token_address) to ensure safe retries without duplicate transactions.

## Project Structure

```
SweepAI/
├── config/
│   ├── config.example.toml    # Template configuration
│   ├── config.test.toml       # Test configuration (Sepolia)
│   └── config.toml            # User configuration (gitignored)
├── data/
│   └── sweepai.db             # SQLite audit trail (gitignored)
├── src/
│   └── sweepai/
│       ├── adapters/
│       │   ├── balance.py     # Blockchain balance reader (RPC)
│       │   └── keeperhub.py   # KeeperHub execution adapter
│       ├── cli/
│       │   └── main.py        # Click CLI entry point
│       ├── core/
│       │   ├── config.py      # Configuration management
│       │   ├── database.py    # SQLite async persistence
│       │   ├── models.py      # Data models and state machine
│       │   └── policy.py      # Deterministic policy engine
│       └── workflow/
│           ├── graph.py       # LangGraph workflow builder
│           └── nodes.py       # Workflow nodes
├── tests/                     # Test suite
├── .env.example               # Environment variable template
├── pyproject.toml             # Build config and dependencies
└── README.md
```

## Supported Chains

| Chain | ID | RPC |
|-------|----|-----|
| Ethereum Mainnet | 1 | `https://eth.llamarpc.com` |
| Sepolia | 11155111 | Configurable via `RPC_URL_11155111` |
| Base | 8453 | `https://mainnet.base.org` |
| Base Sepolia | 84532 | `https://sepolia.base.org` |
| Arbitrum | 42161 | `https://arb1.arbitrum.io/rpc` |
| Polygon | 137 | `https://polygon-rpc.com` |

Custom RPC endpoints can be set per chain using `RPC_URL_<chainId>` environment variables.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linter
ruff check src/sweepai/

# Run type checker
mypy src/sweepai/

# Run tests
pytest
```

## License

MIT
