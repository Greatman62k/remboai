import os
import shutil
from pathlib import Path

import pytest

from app.db import MIGRATIONS, make_pool, migrate

DSN = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


async def _migrate_before_044(pool, directory: Path):
    directory.mkdir()
    for path in MIGRATIONS.glob("0*.sql"):
        if int(path.name[:3]) < 44:
            shutil.copy2(path, directory / path.name)
    await migrate(pool, directory)


async def _add_new_migrations(directory: Path):
    for path in MIGRATIONS.glob("0*.sql"):
        if int(path.name[:3]) >= 44:
            shutil.copy2(path, directory / path.name)


@needs_db
@pytest.mark.asyncio
async def test_upgrade_backfills_only_the_trusted_handoff_message_not_diagnostic_traces(tmp_path):
    pool = await make_pool(DSN)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    directory = tmp_path / "migrations"
    await _migrate_before_044(pool, directory)
    salon = await pool.fetchval("INSERT INTO salons (name,wa_phone_number_id) VALUES ('A','PID-A') RETURNING id::text")
    client = await pool.fetchval("INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,'254700000030') RETURNING id::text", salon)
    handoff = await pool.fetchval(
        "INSERT INTO handoffs (salon_id,client_id,category,message) VALUES ($1::uuid,$2::uuid,'complaint','Customer asked for help') RETURNING id::text",
        salon, client)
    await pool.execute(
        "INSERT INTO conversation_trace (message_id,thread,channel,message_kind,user_text) "
        "VALUES ('diagnostic-old','thread','whatsapp','text','unclassified diagnostic content')")
    await _add_new_migrations(directory)
    await migrate(pool, directory)
    events = await pool.fetch("SELECT event_type,actor_type,visibility,body,sequence FROM handoff_events WHERE handoff_id=$1::uuid", handoff)
    assert len(events) == 1
    assert tuple(events[0]) == ("customer_message", "customer", "customer_visible", "Customer asked for help", 1)
    assert await pool.fetchval("SELECT event_seq FROM handoffs WHERE id=$1::uuid", handoff) == 1
    assert await pool.fetchval("SELECT count(*) FROM handoff_events WHERE body='unclassified diagnostic content'") == 0
    await pool.close()


@needs_db
@pytest.mark.asyncio
async def test_upgrade_fails_clearly_and_without_repair_when_handoff_client_belongs_to_another_salon(tmp_path):
    pool = await make_pool(DSN)
    await pool.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    directory = tmp_path / "migrations"
    await _migrate_before_044(pool, directory)
    salon_a = await pool.fetchval("INSERT INTO salons (name,wa_phone_number_id) VALUES ('A','PID-A') RETURNING id::text")
    salon_b = await pool.fetchval("INSERT INTO salons (name,wa_phone_number_id) VALUES ('B','PID-B') RETURNING id::text")
    client = await pool.fetchval("INSERT INTO clients (salon_id,wa_id) VALUES ($1::uuid,'254700000031') RETURNING id::text", salon_a)
    handoff = await pool.fetchval(
        "INSERT INTO handoffs (salon_id,client_id,category,message) VALUES ($1::uuid,$2::uuid,'complaint','mismatch') RETURNING id::text",
        salon_b, client)
    await _add_new_migrations(directory)
    with pytest.raises(Exception, match="migration 044 aborted: handoffs contain a missing client or a client from another salon"):
        await migrate(pool, directory)
    assert await pool.fetchval("SELECT count(*) FROM handoffs WHERE id=$1::uuid AND salon_id=$2::uuid AND client_id=$3::uuid", handoff, salon_b, client) == 1
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE name='044_handoff_events.sql'") == 0
    await pool.close()
