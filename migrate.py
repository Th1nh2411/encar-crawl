"""
migrate.py — Apply the SQL files in migrations/ to the configured database.

Every migration is applied at most once: applied filenames are recorded in
`schema_migrations` along with a checksum of the file, so an already-migrated
database is a no-op and an edited-after-apply migration is reported instead of
silently ignored.

The files themselves are also written to be idempotent (IF NOT EXISTS /
CREATE OR REPLACE / information_schema guards), so `--force` can safely replay
one against a database whose tracking table was lost.

Nothing here drops anything. Rebuilding from scratch is a deliberate,
destructive act and stays a manual one.

Usage
-----
    python migrate.py                 # apply what is pending
    python migrate.py --status        # show applied / pending, no writes
    python migrate.py --dry-run       # list what would run
    python migrate.py --force 003_vehicles_sold_status.sql   # replay one file
"""

import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from typing import List, Tuple

from encar_stock_sync import HERE, connect, log

MIGRATIONS_DIR = os.path.join(HERE, "migrations")

TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   VARCHAR(191) NOT NULL,
    checksum   CHAR(64)     NOT NULL COMMENT 'sha256 of the file as applied',
    applied_at DATETIME     NOT NULL,
    PRIMARY KEY (filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def checksum(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def migration_files() -> List[str]:
    if not os.path.isdir(MIGRATIONS_DIR):
        raise SystemExit(f"No migrations directory: {MIGRATIONS_DIR}")
    return sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))


def applied_map(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(TRACKING_TABLE_SQL)
        conn.commit()
        cur.execute("SELECT filename, checksum, applied_at FROM schema_migrations")
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def run_sql_file(path: str) -> None:
    """Feed the file to the mysql client.

    Splitting SQL in Python breaks on comments, quoted semicolons and PREPARE
    blocks; the client already parses all of that correctly.
    """
    cmd = [
        "mysql",
        f"--host={os.getenv('DB_HOST', '127.0.0.1')}",
        f"--port={os.getenv('DB_PORT', '3306')}",
        f"--user={os.getenv('DB_USERNAME', 'root')}",
        os.getenv("DB_NAME", "encar"),
    ]
    env = dict(os.environ)
    # Never put the password on argv - it would show up in `ps`.
    env["MYSQL_PWD"] = os.getenv("DB_PASSWORD", "")

    with open(path, "rb") as f:
        proc = subprocess.run(cmd, stdin=f, env=env, capture_output=True)

    stderr = proc.stderr.decode("utf-8", "replace")
    # The client warns about MYSQL_PWD on every run; that is not a failure.
    noise = [ln for ln in stderr.splitlines()
             if ln.strip() and "Using a password" not in ln]
    if proc.returncode != 0:
        raise RuntimeError("\n".join(noise) or f"mysql exited {proc.returncode}")
    for line in noise:
        log(f"      {line}")


def record(conn, filename: str, digest: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (filename, checksum, applied_at) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE checksum = VALUES(checksum), applied_at = VALUES(applied_at)",
            (filename, digest, datetime.now()),
        )
    conn.commit()


def classify(files: List[str], applied: dict) -> List[Tuple[str, str, str]]:
    """-> [(filename, state, digest)] where state is applied / pending / changed"""
    out = []
    for name in files:
        digest = checksum(os.path.join(MIGRATIONS_DIR, name))
        if name not in applied:
            out.append((name, "pending", digest))
        elif applied[name][0] != digest:
            out.append((name, "changed", digest))
        else:
            out.append((name, "applied", digest))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply pending SQL migrations.")
    parser.add_argument("--status", action="store_true", help="Show state only, apply nothing")
    parser.add_argument("--dry-run", action="store_true", help="List what would be applied")
    parser.add_argument("--force", action="append", default=[], metavar="FILE",
                        help="Re-apply this migration even if it is recorded (repeatable)")
    args = parser.parse_args()

    files = migration_files()
    if not files:
        raise SystemExit("No .sql files in migrations/")

    conn = connect()
    try:
        applied = applied_map(conn)
        states = classify(files, applied)

        log(f"{'MIGRATION':<44}{'STATE':<10}APPLIED AT")
        log("-" * 78)
        for name, state, _ in states:
            when = applied.get(name, (None, None))[1]
            log(f"{name:<44}{state:<10}{when or '-'}")
        log("-" * 78)

        if args.status:
            return 0

        forced = set(args.force)
        unknown = forced - set(files)
        if unknown:
            raise SystemExit(f"Unknown migration(s): {', '.join(sorted(unknown))}")

        todo = [(n, d) for n, s, d in states if s != "applied" or n in forced]
        changed = [n for n, s, _ in states if s == "changed" and n not in forced]
        if changed:
            log(f"\n! Edited after being applied: {', '.join(changed)}")
            log("  They will be re-run — the files are written to be idempotent.")

        if not todo:
            log("\nNothing to do — database is up to date.")
            return 0

        if args.dry_run:
            log("\nWould apply: " + ", ".join(n for n, _ in todo))
            return 0

        log("")
        for name, digest in todo:
            log(f"  applying {name} ...")
            try:
                run_sql_file(os.path.join(MIGRATIONS_DIR, name))
            except (RuntimeError, FileNotFoundError) as exc:
                log(f"  !! {name} failed: {exc}")
                return 1
            record(conn, name, digest)
            log(f"  ok {name}")

        log(f"\nApplied {len(todo)} migration(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
