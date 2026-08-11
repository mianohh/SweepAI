"""LangGraph workflow nodes for SweepAI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sweepai.adapters.balance import BalanceInfo, BalanceReader
from sweepai.adapters.keeperhub import ExecutionResult, KeeperHubExecutionAdapter
from sweepai.core.config import AppConfig
from sweepai.core.database import Database
from sweepai.core.models import (
    AuditRecord,
    SweepProposal,
    SweepState,
    TreasuryPolicy,
    WalletConfig,
)
from sweepai.core.policy import PolicyEngine, PolicyError

logger = logging.getLogger("sweepai.workflow")


@dataclass
class WorkflowState:
    """State passed through the workflow graph."""

    config: AppConfig | None = None
    wallet: WalletConfig | None = None
    policy: TreasuryPolicy | None = None
    balance: BalanceInfo | None = None
    proposal: SweepProposal | None = None
    last_sweep_timestamp: float | None = None
    execution_result: ExecutionResult | None = None
    error: str | None = None
    phase: str = "init"


async def _check_paused(config: AppConfig | None) -> str | None:
    """Check if the system is paused. Returns error message if paused, None otherwise."""
    if config is None:
        return None
    db = Database(config.db_path)
    try:
        await db.connect()
        records = await db.list_audit_records(limit=1)
        if records and records[0].status == SweepState.PAUSED:
            return "System is paused. Use 'sweepai unpause' to resume."
    except Exception:
        pass
    finally:
        await db.close()
    return None


async def load_policy_node(state: WorkflowState) -> dict[str, Any]:
    """Load policy from configuration."""
    logger.info("Loading policy from configuration")

    if state.config is None:
        logger.error("No configuration loaded")
        return {"error": "No configuration loaded", "phase": "error"}

    # Check if system is paused
    pause_error = await _check_paused(state.config)
    if pause_error:
        logger.warning("System is paused")
        return {"error": pause_error, "phase": "paused"}

    wallet = state.config.wallet
    policy = state.config.policy
    logger.info(
        "Policy loaded: wallet=%s, threshold=%s, chain=%s",
        wallet.address[:10] + "...",
        policy.sweep_threshold,
        wallet.chain_id,
    )

    # Look up last sweep timestamp for cooldown enforcement
    last_sweep_ts: float | None = None
    db = Database(state.config.db_path)
    try:
        await db.connect()
        last_sweep_ts = await db.get_last_sweep_timestamp()
    except Exception:
        logger.debug("Could not retrieve last sweep timestamp for cooldown")
    finally:
        await db.close()

    return {
        "wallet": wallet,
        "policy": policy,
        "last_sweep_timestamp": last_sweep_ts,
        "phase": "policy_loaded",
    }


async def read_balances_node(state: WorkflowState) -> dict[str, Any]:
    """Read wallet balances from blockchain."""
    if state.wallet is None:
        logger.error("No wallet configured")
        return {"error": "No wallet configured", "phase": "error"}

    logger.info(
        "Reading balance for %s on chain %s",
        state.wallet.address[:10] + "...",
        state.wallet.chain_id,
    )

    reader = BalanceReader()
    try:
        balance = await reader.read_balance(
            address=state.wallet.address,
            chain_id=state.wallet.chain_id,
            token_address=state.wallet.token_address,
        )
        logger.info(
            "Balance read: %s (address=%s)",
            balance.balance,
            balance.address[:10] + "...",
        )
        return {"balance": balance, "phase": "balances_read"}
    except Exception as e:
        logger.error("Failed to read balance: %s", e)
        return {"error": f"Failed to read balance: {e}", "phase": "error"}
    finally:
        await reader.close()


async def analyze_node(state: WorkflowState) -> dict[str, Any]:
    """Analyze whether to sweep using deterministic policy engine."""
    if state.balance is None or state.policy is None:
        logger.error("Missing balance or policy data for analysis")
        return {"error": "Missing balance or policy data", "phase": "error"}

    logger.info(
        "Analyzing sweep: balance=%s, threshold=%s",
        state.balance.balance,
        state.policy.sweep_threshold,
    )

    policy_engine = PolicyEngine(state.policy)
    sweep_amount = policy_engine.calculate_sweep_amount(state.balance.balance)

    if sweep_amount is None:
        logger.info(
            "No sweep needed: balance %s <= threshold %s",
            state.balance.balance,
            state.policy.sweep_threshold,
        )
        return {
            "phase": "no_sweep",
            "validation_warnings": [
                "Balance below sweep threshold or insufficient for minimum"
            ],
        }

    if state.wallet is None:
        return {"error": "No wallet configured", "phase": "error"}

    proposal = SweepProposal(
        source_address=state.wallet.address,
        destination_address=state.policy.allowed_destinations[0],
        amount=sweep_amount,
        chain_id=state.wallet.chain_id,
        token_address=state.wallet.token_address,
        reason=(
            f"Balance {state.balance.balance} exceeds threshold "
            f"{state.policy.sweep_threshold}"
        ),
        status=SweepState.PROPOSED,
    )

    logger.info(
        "Sweep proposed: amount=%s, destination=%s",
        sweep_amount,
        proposal.destination_address[:10] + "...",
    )

    return {"proposal": proposal, "phase": "proposed"}


async def validate_proposal_node(state: WorkflowState) -> dict[str, Any]:
    """
    Validate proposal against policy.

    This is the DETERMINISTIC gate - LLM output is validated here.
    """
    if state.proposal is None or state.policy is None or state.balance is None:
        logger.error("Missing proposal, policy, or balance for validation")
        return {"error": "Missing proposal, policy, or balance", "phase": "error"}

    logger.info(
        "Validating proposal: amount=%s, chain=%s",
        state.proposal.amount,
        state.proposal.chain_id,
    )

    policy_engine = PolicyEngine(state.policy)

    try:
        warnings = policy_engine.validate_proposal(
            proposal=state.proposal,
            current_balance=state.balance.balance,
            last_sweep_timestamp=state.last_sweep_timestamp,
        )
        state.proposal.approve()
        logger.info(
            "Proposal approved: amount=%s, destination=%s",
            state.proposal.amount,
            state.proposal.destination_address[:10] + "...",
        )
        return {
            "proposal": state.proposal,
            "validation_warnings": warnings,
            "phase": "approved",
        }
    except PolicyError as e:
        state.proposal.reject(str(e))
        logger.warning("Proposal rejected: %s", e)
        return {
            "proposal": state.proposal,
            "validation_errors": [str(e)],
            "phase": "rejected",
        }


async def execute_node(state: WorkflowState) -> dict[str, Any]:
    """
    Execute the approved sweep via KeeperHub.

    Uses simulation-first approach.
    """
    if state.proposal is None or state.config is None:
        logger.error("No proposal or config to execute")
        return {"error": "No proposal to execute", "phase": "error"}

    if state.proposal.status != SweepState.APPROVED:
        logger.error("Proposal not approved: status=%s", state.proposal.status)
        return {"error": "Proposal not approved", "phase": "error"}

    logger.info(
        "Executing sweep: amount=%s, chain=%s, destination=%s",
        state.proposal.amount,
        state.proposal.chain_id,
        state.proposal.destination_address[:10] + "...",
    )

    adapter = KeeperHubExecutionAdapter(state.config.keeperhub)

    try:
        # Step 1: Simulate
        logger.info("Step 1: Simulating transfer")
        sim_result = await adapter.simulate_transfer(
            chain_id=state.proposal.chain_id,
            recipient=state.proposal.destination_address,
            amount=state.proposal.amount,
            token_address=state.proposal.token_address,
        )

        if not sim_result.success or sim_result.would_revert:
            error_msg = sim_result.revert_reason or "Simulation failed"
            logger.error("Simulation failed: %s", error_msg)
            return {
                "error": f"Simulation failed: {error_msg}",
                "phase": "sim_failed",
            }

        logger.info(
            "Simulation passed: gas_estimate=%s",
            sim_result.gas_estimate,
        )

        # Step 2: Execute
        logger.info("Step 2: Executing transfer")
        exec_result = await adapter.execute_transfer(
            chain_id=state.proposal.chain_id,
            recipient=state.proposal.destination_address,
            amount=state.proposal.amount,
            token_address=state.proposal.token_address,
            task_id=f"sweep-{state.proposal.id}",
        )

        if exec_result.status == "failed":
            logger.error("Execution failed: %s", exec_result.error)
            return {
                "error": f"Execution failed: {exec_result.error}",
                "execution_result": exec_result,
                "phase": "exec_failed",
            }

        logger.info(
            "Execution submitted: execution_id=%s, tx=%s",
            exec_result.execution_id,
            exec_result.transaction_hash or "pending",
        )

        # Step 3: Wait for confirmation
        logger.info("Step 3: Waiting for confirmation")
        final_result = await adapter.wait_for_completion(exec_result.execution_id)

        logger.info(
            "Sweep executed: status=%s, tx=%s",
            final_result.status,
            final_result.transaction_hash or "N/A",
        )

        return {
            "execution_result": final_result,
            "phase": "executed",
        }

    except Exception as e:
        logger.error("Execution error: %s", e)
        return {"error": f"Execution error: {e}", "phase": "error"}
    finally:
        await adapter.close()


async def audit_node(state: WorkflowState) -> dict[str, Any]:
    """Record audit trail for the sweep operation."""
    if state.proposal is None:
        logger.info("No proposal to audit")
        return {"phase": "audit_complete"}

    logger.info("Recording audit trail for proposal %s", state.proposal.id[:8])

    # Create audit record
    exec_result = state.execution_result
    record = AuditRecord(
        proposal_id=state.proposal.id,
        execution_id=exec_result.execution_id if exec_result else None,
        transaction_hash=exec_result.transaction_hash if exec_result else None,
        status=state.proposal.status,
        error=state.error,
    )

    # Save to database
    if state.config:
        db = Database(state.config.db_path)
        try:
            await db.connect()
            await db.save_proposal(state.proposal)
            await db.save_audit(record)
            logger.info(
                "Audit record saved: proposal_id=%s, status=%s, tx=%s",
                state.proposal.id[:8],
                state.proposal.status.value,
                exec_result.transaction_hash if exec_result else "N/A",
            )
        except Exception as e:
            logger.error("Failed to save audit record: %s", e)
        finally:
            await db.close()

    return {"audit_record": record, "phase": "audit_complete"}


def build_sweep_graph() -> CompiledStateGraph[WorkflowState]:
    """Build the LangGraph workflow for sweep operations."""
    logger.info("Building sweep workflow graph")

    graph = StateGraph(WorkflowState)

    # Add nodes
    graph.add_node("load_policy", load_policy_node)
    graph.add_node("read_balances", read_balances_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("validate_proposal", validate_proposal_node)
    graph.add_node("execute", execute_node)
    graph.add_node("audit", audit_node)

    # Set entry point
    graph.set_entry_point("load_policy")

    # Add edges
    graph.add_edge("load_policy", "read_balances")
    graph.add_edge("read_balances", "analyze")
    graph.add_edge("analyze", "validate_proposal")

    # Conditional edge after validation
    def should_execute(state: WorkflowState) -> Literal["execute", "audit"]:
        if state.proposal and state.proposal.status == SweepState.APPROVED:
            return "execute"
        return "audit"

    graph.add_conditional_edges(
        "validate_proposal",
        should_execute,
        {"execute": "execute", "audit": "audit"},
    )
    graph.add_edge("execute", "audit")
    graph.add_edge("audit", END)

    return graph.compile()
