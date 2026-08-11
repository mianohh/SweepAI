"""SweepAI CLI main entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from sweepai.core.config import AppConfig, load_config
from sweepai.core.database import Database
from sweepai.core.models import SweepState

# Load .env file at startup
load_dotenv()

console = Console()

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run async function from sync CLI."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@click.group()
@click.option("--config", "-c", default="config/config.toml", help="Config file path")
@click.pass_context
def cli(ctx: click.Context, config: str) -> None:
    """SweepAI - Autonomous Treasury Management Agent."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Verify configuration and connections."""
    config_path = ctx.obj["config_path"]

    console.print("[bold]SweepAI Doctor[/bold]\n")

    # Check config file
    if not Path(config_path).exists():
        console.print(f"[red]✗ Config file not found: {config_path}[/red]")
        console.print("  Copy config/config.example.toml to config/config.toml")
        return

    console.print(f"[green]✓ Config file: {config_path}[/green]")

    # Load config
    try:
        config = load_config(config_path)
        addr = config.wallet.address
        console.print(f"[green]✓ Wallet: {addr[:10]}...{addr[-8:]}[/green]")
        console.print(f"[green]✓ Chain: {config.wallet.chain_id}[/green]")
        console.print(f"[green]✓ Threshold: {config.policy.sweep_threshold}[/green]")
    except Exception as e:
        console.print(f"[red]✗ Config error: {e}[/red]")
        return

    # Check KeeperHub API key
    import os
    if os.environ.get("KEEPERHUB_API_KEY"):
        console.print("[green]✓ KEEPERHUB_API_KEY set[/green]")
    else:
        console.print("[yellow]⚠ KEEPERHUB_API_KEY not set[/yellow]")

    # Check database
    db = Database(config.db_path)
    try:
        run_async(db.connect())
        console.print(f"[green]✓ Database: {config.db_path}[/green]")
    except Exception as e:
        console.print(f"[red]✗ Database error: {e}[/red]")
    finally:
        run_async(db.close())

    console.print("\n[bold green]Doctor check complete[/bold green]")


@cli.command()
@click.pass_context
def observe(ctx: click.Context) -> None:
    """Read current wallet balance."""
    config = _load_config(ctx)

    from sweepai.adapters.balance import BalanceReader

    reader = BalanceReader()
    try:
        balance = run_async(reader.read_balance(
            address=config.wallet.address,
            chain_id=config.wallet.chain_id,
            token_address=config.wallet.token_address,
        ))

        table = Table(title="Wallet Balance")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Address", balance.address)
        table.add_row("Chain", balance.chain_id)
        table.add_row("Balance", f"{balance.balance}")
        if balance.token_address:
            table.add_row("Token", balance.token_address)

        console.print(table)

        # Check against threshold
        from decimal import Decimal
        threshold = config.policy.sweep_threshold
        if Decimal(balance.balance) > Decimal(threshold):
            console.print(f"\n[bold yellow]⚠ Balance exceeds threshold ({threshold})[/bold yellow]")
        else:
            console.print(f"\n[green]Balance below sweep threshold ({threshold})[/green]")

    except Exception as e:
        console.print(f"[red]Error reading balance: {e}[/red]")
    finally:
        run_async(reader.close())


@cli.command()
@click.pass_context
def evaluate(ctx: click.Context) -> None:
    """Run analysis on whether to sweep."""
    config = _load_config(ctx)

    from sweepai.workflow.nodes import (
        WorkflowState,
        analyze_node,
        load_policy_node,
        read_balances_node,
    )

    async def _evaluate() -> dict[str, Any]:
        state = WorkflowState(config=config)
        result = await load_policy_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        result = await read_balances_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        return await analyze_node(state)

    result = run_async(_evaluate())

    if result.get("error"):
        console.print(f"[red]Error: {result['error']}[/red]")
        return

    phase = result.get("phase", "unknown")
    if phase == "no_sweep":
        console.print("[green]No sweep recommended[/green]")
        for w in result.get("validation_warnings", []):
            console.print(f"  - {w}")
    elif phase == "proposed":
        proposal = result.get("proposal")
        if proposal:
            console.print("[bold yellow]Sweep Recommended[/bold yellow]")
            console.print(f"  Amount: {proposal.amount}")
            console.print(f"  Destination: {proposal.destination_address[:10]}...")
            console.print(f"  Reason: {proposal.reason}")
    else:
        console.print(f"[yellow]Phase: {phase}[/yellow]")


@cli.command()
@click.pass_context
def propose(ctx: click.Context) -> None:
    """Generate and store a sweep proposal."""
    config = _load_config(ctx)

    from sweepai.workflow.nodes import (
        WorkflowState,
        analyze_node,
        load_policy_node,
        read_balances_node,
        validate_proposal_node,
    )

    async def _propose() -> WorkflowState:
        state = WorkflowState(config=config)
        result = await load_policy_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        result = await read_balances_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        result = await analyze_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        if state.proposal:
            result = await validate_proposal_node(state)
            for k, v in result.items():
                setattr(state, k, v)
        return state

    state = run_async(_propose())

    if state.error:
        console.print(f"[red]Error: {state.error}[/red]")
        return

    if state.proposal:
        # Save to database
        db = Database(config.db_path)
        try:
            run_async(db.connect())
            run_async(db.save_proposal(state.proposal))
        finally:
            run_async(db.close())

        console.print("[bold green]Proposal Created[/bold green]")
        console.print(f"  ID: {state.proposal.id}")
        console.print(f"  Amount: {state.proposal.amount}")
        console.print(f"  Status: {state.proposal.status.value}")
    else:
        console.print("[yellow]No sweep proposal generated[/yellow]")


@cli.command()
@click.argument("proposal_id")
@click.pass_context
def approve(ctx: click.Context, proposal_id: str) -> None:
    """Approve a pending proposal for execution."""
    config = _load_config(ctx)

    db = Database(config.db_path)
    try:
        run_async(db.connect())
        proposal = run_async(db.get_proposal(proposal_id))

        if not proposal:
            console.print(f"[red]Proposal not found: {proposal_id}[/red]")
            return

        if proposal.status != SweepState.PROPOSED:
            console.print(f"[red]Cannot approve proposal in state: {proposal.status.value}[/red]")
            return

        proposal.approve()
        run_async(db.save_proposal(proposal))

        console.print(f"[green]Proposal {proposal_id} approved[/green]")
        console.print(f"  Amount: {proposal.amount}")
        console.print(f"  Destination: {proposal.destination_address[:10]}...")

    finally:
        run_async(db.close())


@cli.command()
@click.argument("proposal_id")
@click.pass_context
def execute(ctx: click.Context, proposal_id: str) -> None:
    """Execute an approved proposal via KeeperHub."""
    config = _load_config(ctx)

    db = Database(config.db_path)
    try:
        run_async(db.connect())
        proposal = run_async(db.get_proposal(proposal_id))

        if not proposal:
            console.print(f"[red]Proposal not found: {proposal_id}[/red]")
            return

        if proposal.status != SweepState.APPROVED:
            console.print(f"[red]Cannot execute proposal in state: {proposal.status.value}[/red]")
            return

        console.print(f"[bold]Executing sweep: {proposal.amount}[/bold]")

        # Execute via KeeperHub
        from sweepai.adapters.keeperhub import ExecutionResult, KeeperHubExecutionAdapter

        adapter = KeeperHubExecutionAdapter(config.keeperhub)

        async def _execute() -> ExecutionResult | None:
            # Simulate first
            console.print("[yellow]Simulating...[/yellow]")
            sim = await adapter.simulate_transfer(
                chain_id=proposal.chain_id,
                recipient=proposal.destination_address,
                amount=proposal.amount,
                token_address=proposal.token_address,
            )

            if not sim.success or sim.would_revert:
                console.print(f"[red]Simulation failed: {sim.revert_reason}[/red]")
                return None

            console.print("[green]Simulation passed[/green]")

            # Execute
            console.print("[yellow]Executing...[/yellow]")
            result = await adapter.execute_transfer(
                chain_id=proposal.chain_id,
                recipient=proposal.destination_address,
                amount=proposal.amount,
                token_address=proposal.token_address,
                task_id=f"sweep-{proposal.id}",
            )

            if result.status == "failed":
                console.print(f"[red]Execution failed: {result.error}[/red]")
                return None

            # Wait for completion
            final = await adapter.wait_for_completion(result.execution_id)
            return final

        try:
            result = run_async(_execute())
        finally:
            run_async(adapter.close())

        if result:
            new_status = SweepState.CONFIRMED if result.status == "completed" else SweepState.FAILED
            proposal.status = new_status
            run_async(db.save_proposal(proposal))

            if result.status == "completed":
                console.print("[bold green]Sweep Complete[/bold green]")
                console.print(f"  TX: {result.transaction_hash}")
                console.print(f"  Link: {result.transaction_link}")
            else:
                console.print(f"[red]Sweep failed: {result.error}[/red]")

    finally:
        run_async(db.close())


@cli.command()
@click.argument("execution_id", required=False)
@click.pass_context
def status(ctx: click.Context, execution_id: str | None) -> None:
    """Check execution status."""
    config = _load_config(ctx)

    from sweepai.adapters.keeperhub import KeeperHubExecutionAdapter

    if not execution_id:
        console.print("[yellow]Provide an execution ID to check status[/yellow]")
        return

    adapter = KeeperHubExecutionAdapter(config.keeperhub)
    try:
        result = run_async(adapter.get_execution_status(execution_id))

        table = Table(title=f"Execution {execution_id}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Status", result.status)
        if result.transaction_hash:
            table.add_row("TX Hash", result.transaction_hash)
        if result.transaction_link:
            table.add_row("Link", result.transaction_link)
        if result.error:
            table.add_row("Error", result.error)

        console.print(table)
    finally:
        run_async(adapter.close())


@cli.command()
@click.option("--limit", "-n", default=20, help="Number of records to show")
@click.pass_context
def audit(ctx: click.Context, limit: int) -> None:
    """View audit trail."""
    config = _load_config(ctx)

    db = Database(config.db_path)
    try:
        run_async(db.connect())
        records = run_async(db.list_audit_records(limit=limit))
        proposals = run_async(db.list_proposals())
    finally:
        run_async(db.close())

    if not proposals and not records:
        console.print("[yellow]No audit records found[/yellow]")
        return

    # Proposals table
    if proposals:
        table = Table(title="Sweep Proposals")
        table.add_column("ID", style="cyan")
        table.add_column("Amount", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Timestamp")

        for p in proposals[:limit]:
            table.add_row(
                p.id[:8] + "...",
                p.amount,
                p.status.value,
                p.timestamp.strftime("%Y-%m-%d %H:%M"),
            )

        console.print(table)

    # Audit records table
    if records:
        table = Table(title="Audit Records")
        table.add_column("ID", style="cyan")
        table.add_column("Proposal", style="green")
        table.add_column("TX Hash", style="yellow")
        table.add_column("Status")
        table.add_column("Timestamp")

        for r in records[:limit]:
            table.add_row(
                r.id[:8] + "...",
                r.proposal_id[:8] + "...",
                (r.transaction_hash[:10] + "...") if r.transaction_hash else "-",
                r.status.value,
                r.timestamp.strftime("%Y-%m-%d %H:%M"),
            )

        console.print(table)


@cli.command()
@click.pass_context
def pause(ctx: click.Context) -> None:
    """Emergency pause all operations."""
    config = _load_config(ctx)

    # Write pause flag to database
    db = Database(config.db_path)
    try:
        run_async(db.connect())
        from sweepai.core.models import AuditRecord
        record = AuditRecord(
            proposal_id="system",
            status=SweepState.PAUSED,
            metadata={"action": "pause", "reason": "Manual pause via CLI"},
        )
        run_async(db.save_audit(record))
        console.print("[bold red]PAUSED[/bold red] All sweep operations are paused.")
    finally:
        run_async(db.close())


@cli.command()
@click.pass_context
def unpause(ctx: click.Context) -> None:
    """Resume operations after pause."""
    config = _load_config(ctx)

    db = Database(config.db_path)
    try:
        run_async(db.connect())
        from sweepai.core.models import AuditRecord
        record = AuditRecord(
            proposal_id="system",
            status=SweepState.IDLE,
            metadata={"action": "unpause", "reason": "Manual unpause via CLI"},
        )
        run_async(db.save_audit(record))
        console.print("[bold green]UNPAUSED[/bold green] Sweep operations resumed.")
    finally:
        run_async(db.close())


def _load_config(ctx: click.Context) -> AppConfig:
    """Load configuration from context."""
    config_path = ctx.obj.get("config_path", "config/config.toml")
    return load_config(config_path)


@cli.command()
@click.option("--interval", "-i", default=3600, help="Seconds between sweeps")
@click.pass_context
def cron(ctx: click.Context, interval: int) -> None:
    """Run periodic sweep checks (for systemd/cron)."""
    import time as time_module

    config = _load_config(ctx)
    console.print(f"[bold]SweepAI Cron starting (interval: {interval}s)[/bold]")

    while True:
        try:
            _run_sweep_cycle(config)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

        time_module.sleep(interval)


def _run_sweep_cycle(config: AppConfig) -> None:
    """Run a single sweep cycle."""
    from sweepai.workflow.nodes import (
        WorkflowState,
        analyze_node,
        audit_node,
        execute_node,
        load_policy_node,
        read_balances_node,
        validate_proposal_node,
    )

    async def _cycle() -> str | None:
        state = WorkflowState(config=config)
        result = await load_policy_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        if state.error:
            return state.error

        result = await read_balances_node(state)
        for k, v in result.items():
            setattr(state, k, v)
        if state.error:
            return state.error

        result = await analyze_node(state)
        for k, v in result.items():
            setattr(state, k, v)

        if state.proposal:
            result = await validate_proposal_node(state)
            for k, v in result.items():
                setattr(state, k, v)

            if state.proposal and state.proposal.status.value == "approved":
                result = await execute_node(state)
                for k, v in result.items():
                    setattr(state, k, v)

        await audit_node(state)

        if state.execution_result and state.execution_result.transaction_hash:
            return state.execution_result.transaction_hash
        return None

    tx_hash = run_async(_cycle())
    if tx_hash:
        console.print(f"[bold green]Sweep executed: {tx_hash}[/bold green]")
    else:
        console.print("[dim]No sweep needed[/dim]")


if __name__ == "__main__":
    cli()
