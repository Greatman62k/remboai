from __future__ import annotations

import argparse
import asyncio
import asyncpg
from app.config import get_settings
from app.token_crypto import encrypt_token, valid_encryption_key

async def migrate(apply: bool) -> int:
    settings = get_settings()
    key = settings.token_encryption_key
    if not valid_encryption_key(key):
        raise SystemExit("TOKEN_ENCRYPTION_KEY must be 64 hex characters; generate it with: openssl rand -hex 32")
    conn = await asyncpg.connect(settings.database_url)
    try:
        rows = await conn.fetch("SELECT id, wa_access_token, ig_access_token FROM salons WHERE (wa_access_token IS NOT NULL AND wa_access_token <> '' AND wa_access_token NOT LIKE 'enc:v1:%') OR (ig_access_token IS NOT NULL AND ig_access_token <> '' AND ig_access_token NOT LIKE 'enc:v1:%')")
        if not apply:
            print(f"Dry run: {len(rows)} salon row(s) have plaintext provider tokens; no changes made.")
            return len(rows)
        async with conn.transaction():
            for row in rows:
                await conn.execute("UPDATE salons SET wa_access_token=$2, ig_access_token=$3 WHERE id=$1", row["id"], encrypt_token(row["wa_access_token"], key), encrypt_token(row["ig_access_token"], key))
        print(f"Encrypted provider tokens in {len(rows)} salon row(s).")
        return len(rows)
    finally:
        await conn.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt legacy plaintext salon API tokens in Postgres")
    parser.add_argument("--apply", action="store_true", help="write encrypted tokens to Postgres")
    args = parser.parse_args()
    asyncio.run(migrate(args.apply))

if __name__ == "__main__":
    main()
