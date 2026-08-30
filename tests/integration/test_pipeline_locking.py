from __future__ import annotations

import sqlite3

from akaton.domain.models import CandidateSeed, DocumentContext, ExtractionEnvelope, FetchResult
from akaton.persistence.database import Database
from akaton.pipeline import CandidatePipeline
from akaton.processing.deterministic import extract_deterministically

# Sparse enough that deterministic extraction stays ambiguous and the LLM is consulted.
SPARSE = "A competition is being organised. Details to follow. " * 12


class SparseFetcher:
    async def fetch(self, url, **kwargs):
        return FetchResult(
            requested_url=url,
            final_url=url,
            fetch_method="http",
            status_code=200,
            title="Some competition",
            text=SPARSE,
            content_hash="sparse",
            usable=True,
        )


class LockProbingLLM:
    """Checks whether the pipeline holds a write transaction while the LLM runs.

    A real extraction takes tens of seconds. If that await sits inside an open SQLite
    write transaction, every other candidate processed in parallel blocks on it and
    eventually fails with `database is locked`.
    """

    name = "probe"

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.write_lock_was_held: bool | None = None

    async def extract(self, context: DocumentContext) -> ExtractionEnvelope:
        connection = sqlite3.connect(self.db_path, timeout=0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            self.write_lock_was_held = False
        except sqlite3.OperationalError:
            self.write_lock_was_held = True
        finally:
            connection.close()
        return extract_deterministically(context)


async def test_llm_is_called_without_holding_a_write_transaction(config, tmp_path):
    db_path = tmp_path / "akaton.db"
    database = Database(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await database.create_schema()
    llm = LockProbingLLM(str(db_path))
    pipeline = CandidatePipeline(database, config, SparseFetcher(), llm=llm)

    await pipeline.process(
        CandidateSeed(
            url="https://example.ph/events/some-competition",
            discovery_channel="search",
            provider="fake",
        )
    )

    assert llm.write_lock_was_held is False, (
        "the pipeline held a SQLite write transaction across the LLM call"
    )
    await database.close()
