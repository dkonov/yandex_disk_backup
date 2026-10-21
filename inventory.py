#!/usr/bin/env python3
"""Read-only inventory of multiple rclone remotes with SQLite and CSV reports."""
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import ijson

DB = Path(os.getenv("DATABASE_PATH", "/data/inventory.sqlite3"))
ACCOUNTS = Path(os.getenv("ACCOUNTS_FILE", "/config/accounts.json"))
REPORTS = Path(os.getenv("REPORTS_DIR", "/data/reports"))

DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    current_run TEXT REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS objects (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    is_dir INTEGER NOT NULL,
    size INTEGER,
    modtime TEXT,
    mime_type TEXT,
    md5 TEXT,
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS objects_hash_idx ON objects(md5, size) WHERE md5 IS NOT NULL AND is_dir=0;
CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(account, status);
"""

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

def load_accounts():
    config = json.loads(ACCOUNTS.read_text(encoding="utf-8"))
    items = config.get("accounts")
    if not isinstance(items, list):
        raise ValueError("accounts must be a list")
    names = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid account")
        name, remote = item.get("id"), item.get("remote")
        if not isinstance(name, str) or not name or not isinstance(remote, str) or not remote.endswith(":"):
            raise ValueError("each account needs nonempty id and rclone remote ending with ':'")
        if name in names:
            raise ValueError(f"duplicate account id: {name}")
        names.add(name)
    return items

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(DDL)
    return conn

def normalize_md5(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None

def scan(conn, account):
    name, remote = account["id"], account["remote"]
    run_id = uuid.uuid4().hex
    conn.execute("INSERT OR IGNORE INTO accounts(id) VALUES (?)", (name,))
    conn.execute("INSERT INTO runs(id,account,started,status) VALUES (?,?,?,?)", (run_id, name, now(), "running"))
    conn.commit()
    command = ["rclone", "lsjson", remote, "--recursive", "--hash", "--disable", "ListR"]
    print(f"[{name}] scanning {remote}", flush=True)
    # Stream the JSON array: no need to hold millions of entries in RAM.
    proc = None
    count = 0
    size = 0
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        batch = []
        for obj in ijson.items(proc.stdout, "item"):
            path = obj.get("Path")
            if not isinstance(path, str) or not path:
                continue
            is_dir = bool(obj.get("IsDir", False))
            length = obj.get("Size") if not is_dir else None
            hashes = obj.get("Hashes") or {}
            md5 = normalize_md5(hashes.get("MD5") or hashes.get("md5"))
            batch.append((run_id, path, int(is_dir), length, obj.get("ModTime"),
                          obj.get("MimeType"), None if is_dir else md5))
            if not is_dir:
                count += 1
                size += length or 0
            if len(batch) >= 1000:
                conn.executemany("INSERT INTO objects VALUES (?,?,?,?,?,?,?)", batch)
                conn.commit()
                batch.clear()
        if batch:
            conn.executemany("INSERT INTO objects VALUES (?,?,?,?,?,?,?)", batch)
            conn.commit()
        stderr = proc.stderr.read().decode("utf-8", errors="replace")
        code = proc.wait()
        if code:
            raise RuntimeError(f"rclone exit {code}: {stderr[-3000:]}")
        conn.execute("BEGIN")
        # Publish the new inventory only after the entire scan succeeds.
        previous = conn.execute("SELECT current_run FROM accounts WHERE id=?", (name,)).fetchone()[0]
        conn.execute("UPDATE runs SET status='completed',finished=? WHERE id=?", (now(), run_id))
        conn.execute("UPDATE accounts SET current_run=? WHERE id=?", (run_id, name))
        if previous:
            conn.execute("DELETE FROM objects WHERE run_id=?", (previous,))
            conn.execute("DELETE FROM runs WHERE id=?", (previous,))
        conn.commit()
        print(f"[{name}] finished: {count} files, {size} bytes", flush=True)
    except Exception as exc:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        conn.rollback()
        conn.execute("DELETE FROM objects WHERE run_id=?", (run_id,))
        conn.execute("UPDATE runs SET status='failed',finished=?,error=? WHERE id=?",
                     (now(), str(exc)[:3000], run_id))
        conn.commit()
        print(f"[{name}] ERROR: {exc}", file=sys.stderr, flush=True)
        raise

def reports(conn):
    REPORTS.mkdir(parents=True, exist_ok=True)
    inventory = REPORTS / "inventory.csv"
    with inventory.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["account", "path", "is_dir", "size", "modtime", "mime_type", "md5"])
        for row in conn.execute("""
            SELECT a.id,o.path,o.is_dir,o.size,o.modtime,o.mime_type,o.md5
            FROM accounts a JOIN objects o ON o.run_id=a.current_run
            ORDER BY a.id,o.path
        """):
            writer.writerow(row)
    duplicates = REPORTS / "duplicates.csv"
    query = """
      SELECT a.id, o.path, o.size, o.md5, g.copies, g.potential_saved_bytes
      FROM accounts a JOIN objects o ON o.run_id=a.current_run
      JOIN (
        SELECT o2.md5,o2.size,COUNT(*) AS copies,
               (COUNT(*)-1)*o2.size AS potential_saved_bytes
        FROM accounts a2 JOIN objects o2 ON o2.run_id=a2.current_run
        WHERE o2.is_dir=0 AND o2.md5 IS NOT NULL
        GROUP BY o2.md5,o2.size HAVING COUNT(*)>1
      ) g ON g.md5=o.md5 AND g.size=o.size
      WHERE o.is_dir=0
      ORDER BY g.potential_saved_bytes DESC,o.md5,a.id,o.path
    """
    with duplicates.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["account", "path", "size", "md5", "copies", "potential_saved_bytes"])
        for row in conn.execute(query):
            writer.writerow(row)
    accounts = [
        dict(account=row[0], files=row[1], directories=row[2], total_bytes=row[3],
             with_md5=row[4])
        for row in conn.execute("""
            SELECT a.id,COALESCE(SUM(CASE WHEN o.is_dir=0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN o.is_dir=1 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN o.is_dir=0 THEN o.size ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN o.is_dir=0 AND o.md5 IS NOT NULL THEN 1 ELSE 0 END),0)
            FROM accounts a JOIN objects o ON o.run_id=a.current_run
            GROUP BY a.id ORDER BY a.id
        """)
    ]
    row = conn.execute("""
      SELECT COUNT(*),COALESCE(SUM((copies-1)*size),0)
      FROM (
        SELECT o.md5,o.size,COUNT(*) AS copies
        FROM accounts a JOIN objects o ON o.run_id=a.current_run
        WHERE o.is_dir=0 AND o.md5 IS NOT NULL
        GROUP BY o.md5,o.size HAVING COUNT(*)>1
      )
    """).fetchone()
    summary = {"generated_at": now(), "accounts": accounts,
               "duplicate_groups": row[0], "potential_saved_bytes": row[1],
               "note": "Duplicates are MD5+size candidates; missing MD5 files are excluded. No cloud changes."}
    (REPORTS / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Reports: {REPORTS}", flush=True)

def main():
    parser = argparse.ArgumentParser(description="Read-only Yandex Disk inventory")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan", help="Scan all enabled accounts or one account")
    p.add_argument("--account", help="Account id from accounts.json")
    sub.add_parser("report", help="Regenerate reports from last successful scans")
    args = parser.parse_args()
    conn = connect()
    try:
        if args.command == "scan":
            accounts = load_accounts()
            if args.account:
                accounts = [x for x in accounts if x["id"] == args.account]
                if not accounts:
                    parser.error("unknown account")
            else:
                accounts = [x for x in accounts if x.get("enabled", True)]
            failed = 0
            for account in accounts:
                try:
                    scan(conn, account)
                except Exception:
                    failed += 1
            reports(conn)
            if failed:
                sys.exit(1)
        else:
            reports(conn)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
