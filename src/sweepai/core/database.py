"""SQLite database layer for audit persistence."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

from sweepai.core.models import AuditRecord, SweepProposal, SweepState

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    source_address TEXT NOT NULL,
    destination_address TEXT NOT NULL,
    amount TEXT NOT NULL,
    chain_id TEXT NOT NULL,
    token_address TEXT,
    reason TEXT,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_records (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    execution_id TEXT,
    transaction_hash TEXT,
    status TEXT NOT NULL,
    gas_used TEXT,
    error TEXT,
    timestamp TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_audit_proposal ON audit_records(proposal_id);
CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_records(status);
"""


class Database:
    """Async SQLite database for SweepAI persistence."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def save_proposal(self, proposal: SweepProposal) -> None:
        """Save a sweep proposal."""
        assert self._conn is not None, "Database not connected"
        await self._conn.execute(
            """INSERT OR REPLACE INTO proposals
               (id, source_address, destination_address, amount, chain_id,
                token_address, reason, timestamp, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.id,
                proposal.source_address,
                proposal.destination_address,
                proposal.amount,
                proposal.chain_id,
                proposal.token_address,
                proposal.reason,
                proposal.timestamp.isoformat(),
                proposal.status.value,
            ),
        )
        await self._conn.commit()

    async def get_proposal(self, proposal_id: str) -> SweepProposal | None:
        """Retrieve a proposal by ID."""
        assert self._conn is not None, "Database not connected"
        cursor = await self._conn.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return SweepProposal(
            id=row["id"],
            source_address=row["source_address"],
            destination_address=row["destination_address"],
            amount=row["amount"],
            chain_id=row["chain_id"],
            token_address=row["token_address"],
            reason=row["reason"] or "",
            status=SweepState(row["status"]),
        )

    async def list_proposals(self, status: SweepState | None = None) -> list[SweepProposal]:
        """List proposals, optionally filtered by status."""
        assert self._conn is not None, "Database not connected"
        if status:
            cursor = await self._conn.execute(
                "SELECT * FROM proposals WHERE status = ? ORDER BY timestamp DESC",
                (status.value,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM proposals ORDER BY timestamp DESC"
            )
        rows = await cursor.fetchall()
        return [
            SweepProposal(
                id=row["id"],
                source_address=row["source_address"],
                destination_address=row["destination_address"],
                amount=row["amount"],
                chain_id=row["chain_id"],
                token_address=row["token_address"],
                reason=row["reason"] or "",
                status=SweepState(row["status"]),
            )
            for row in rows
        ]

    async def save_audit(self, record: AuditRecord) -> None:
        """Save an audit record."""
        assert self._conn is not None, "Database not connected"
        import json

        await self._conn.execute(
            """INSERT OR REPLACE INTO audit_records
               (id, proposal_id, execution_id, transaction_hash, status,
                gas_used, error, timestamp, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id,
                record.proposal_id,
                record.execution_id,
                record.transaction_hash,
                record.status.value,
                record.gas_used,
                record.error,
                record.timestamp.isoformat(),
                json.dumps(record.metadata),
            ),
        )
        await self._conn.commit()

    async def get_audit(self, record_id: str) -> AuditRecord | None:
        """Retrieve an audit record by ID."""
        assert self._conn is not None, "Database not connected"
        cursor = await self._conn.execute(
            "SELECT * FROM audit_records WHERE id = ?", (record_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        import json

        return AuditRecord(
            id=row["id"],
            proposal_id=row["proposal_id"],
            execution_id=row["execution_id"],
            transaction_hash=row["transaction_hash"],
            status=SweepState(row["status"]),
            gas_used=row["gas_used"],
            error=row["error"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    async def list_audit_records(self, limit: int = 50) -> list[AuditRecord]:
        """List recent audit records."""
        assert self._conn is not None, "Database not connected"
        cursor = await self._conn.execute(
            "SELECT * FROM audit_records ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        import json

        return [
            AuditRecord(
                id=row["id"],
                proposal_id=row["proposal_id"],
                execution_id=row["execution_id"],
                transaction_hash=row["transaction_hash"],
                status=SweepState(row["status"]),
                gas_used=row["gas_used"],
                error=row["error"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            )
            for row in rows
        ]
