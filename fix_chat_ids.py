#!/usr/bin/env python3
"""
fix_chat_ids.py — One-time database repair script.

Finds every source_groups row where chat_id is a positive integer
(missing the mandatory -100 supergroup prefix) and corrects it.

If the corrected -100 version already exists as a separate row,
the positive-ID duplicate is deleted (keeping the -100 row).

Run once on Railway after deploying the dual-account update:
    python fix_chat_ids.py
"""

import sqlite3
import pathlib
import sys

DB = pathlib.Path(__file__).parent / "data" / "job_bot.db"

if not DB.exists():
    print(f"❌  Database not found at {DB}")
    sys.exit(1)

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

bad_rows = conn.execute(
    "SELECT * FROM source_groups WHERE chat_id > 0 ORDER BY added_at"
).fetchall()

if not bad_rows:
    print("✅  No positive chat_ids found — database is clean.")
    conn.close()
    sys.exit(0)

print(f"Found {len(bad_rows)} row(s) with positive (un-prefixed) chat_id:\n")

fixed = deleted = skipped = 0

for row in bad_rows:
    raw_id     = row["chat_id"]
    correct_id = int(f"-100{raw_id}")
    title      = row["title"]
    account    = row["assigned_account"]

    clash = conn.execute(
        "SELECT chat_id, assigned_account FROM source_groups WHERE chat_id = ?",
        (correct_id,)
    ).fetchone()

    if clash:
        # The -100 version already exists — delete the positive-ID duplicate
        conn.execute("DELETE FROM source_groups WHERE chat_id = ?", (raw_id,))
        print(
            f"  🗑️   Deleted duplicate  │ {title}\n"
            f"       {raw_id} removed  │ kept {correct_id} (Account {clash['assigned_account']})"
        )
        deleted += 1
    else:
        # Correct the ID in-place
        conn.execute(
            "UPDATE source_groups SET chat_id = ? WHERE chat_id = ?",
            (correct_id, raw_id),
        )
        print(
            f"  ✅   Fixed             │ {title}\n"
            f"       {raw_id} → {correct_id}  │ Account {account}"
        )
        fixed += 1

conn.commit()
conn.close()

print(f"\n─────────────────────────────────────")
print(f"  Fixed:   {fixed}")
print(f"  Deleted (duplicates): {deleted}")
print(f"  Skipped: {skipped}")
print(f"─────────────────────────────────────")
print("Done. Restart the bot for changes to take effect.")