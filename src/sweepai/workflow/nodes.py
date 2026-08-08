"""LangGraph workflow nodes for SweepAI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from langgraph.graph import END, StateGraph

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


@dataclass
class WorkflowState:
    """State passed through the workflow graph."""

    config: AppConfig | None = None
    wallet: WalletConfig | None = None
    policy: TreasuryPolicy | None = None
    balance: BalanceInfo | None = None
    proposal: SweepProposal | None = None
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    execution_result: ExecutionResult | None = None
    audit_record: AuditRecord | None = None
    error: str | None = None
    phase: str = "init"


async def load_policy_node(state: WorkflowState) -> dict[str, Any]:
    """Load policy from configuration."""
    if state.config is None:
        return {"error": "No configuration loaded", "phase": "error"}

    return {
        "wallet": state.config.wallet,
        "policy": state.config.policy,
        "phase": "policy_loaded",
    }


async def read_balances_node(state: WorkflowState) -> dict[str, Any]:
    """Read wallet balances from blockchain."""
    if state.wallet is None:
        return {"error": "No wallet configured", "phase": "error"}

    reader = BalanceReader()
    try:
        balance = await reader.read_balance(
            address=state.wallet.address,
            chain_id=state.wallet.chain_id,
            token_address=state.wallet.token_address,
        )
        return {"balance": balance, "phase": "balances_read"}
    except Exception as e:
        return {"error": f"Failed to read balance: {e}", "phase": "error"}
    finally:
        await reader.close()


async def analyze_node(state: WorkflowState) -> dict[str, Any]:
    """Analyze whether to sweep using deterministic policy engine."""
    if state.balance is None or state.policy is None:
        return {"error": "Missing balance or policy data", "phase": "error"}

    policy_engine = PolicyEngine(state.policy)
    sweep_amount = policy_engine.calculate_sweep_amount(state.balance.balance)

    if sweep_amount is None:
        return {
            "phase": "no_sweep",
            "validation_warnings": ["Balance below sweep threshold or insufficient for minimum"],
        }

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
    return {"proposal": proposal, "phase": "proposed"}


async def validate_proposal_node(state: WorkflowState) -> dict[str, Any]:
    """
    Validate proposal against policy.

    This is the DETERMINISTIC gate - LLM output is validated here.
    """
    if state.proposal is None or state.policy is None or state.balance is None:
        return {"error": "Missing proposal, policy, or balance", "phase": "error"}

    policy_engine = PolicyEngine(state.policy)

    try:
        warnings = policy_engine.validate_proposal(
            proposal=state.proposal,
            current_balance=state.balance.balance,
        )
        return {"validation_warnings": warnings, "phase": "validated"}
    except PolicyError as e:
        state.proposal.reject(str(e))
        return {
            "validation_errors": [str(e)],
            "phase": "rejected",
        }


async def execute_node(state: WorkflowState) -> dict[str, Any]:
    """
    Execute the approved sweep via KeeperHub.

    Uses simulation-first approach.
    """
    if state.proposal is None or state.config is None:
        return {"error": "No proposal to execute", "phase": "error"}

    if state.proposal.status != SweepState.APPROVED:
        return {"error": "Proposal not approved", "phase": "error"}

    adapter = KeeperHubExecutionAdapter(state.config.keeperhub)

    try:
        # Step 1: Simulate
        sim_result = await adapter.simulate_transfer(
            chain_id=state.proposal.chain_id,
            recipient=state.proposal.destination_address,
            amount=state.proposal.amount,
            token_address=state.proposal.token_address,
        )

        if not sim_result.success or sim_result.would_revert:
            error_msg = sim_result.revert_reason or "Simulation failed"
            return {
                "error": f"Simulation failed: {error_msg}",
                "phase": "sim_failed",
            }

        # Step 2: Execute
        exec_result = await adapter.execute_transfer(
            chain_id=state.proposal.chain_id,
            recipient=state.proposal.destination_address,
            amount=state.proposal.amount,
            token_address=state.proposal.token_address,
            task_id=f"sweep-{state.proposal.id}",
        )

        if exec_result.status == "failed":
            return {
                "error": f"Execution failed: {exec_result.error}",
                "execution_result": exec_result,
                "phase": "exec_failed",
            }

        # Step 3: Wait for confirmation
        final_result = await adapter.wait_for_completion(exec_result.execution_id)

        return {
            "execution_result": final_result,
            "phase": "executed",
        }

    except Exception as e:
        return {"error": f"Execution error: {e}", "phase": "error"}
    finally:
        await adapter.close()


async def audit_node(state: WorkflowState) -> dict[str, Any]:
    """Record audit trail for the sweep operation."""
    if state.proposal is None:
        return {"phase": "audit_complete"}

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
        finally:
            await db.close()

    return {"audit_record": record, "phase": "audit_complete"}


def build_sweep_graph() -> StateGraph:
    """Build the LangGraph workflow for sweep operations."""
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
        "validate_proposal", should_execute, {"execute": "execute", "audit": "audit"}
    )
    graph.add_edge("execute", "audit")
    graph.add_edge("audit", END)

    return graph.compile()
