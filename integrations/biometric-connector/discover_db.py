#!/usr/bin/env python3
"""One-off: inspect the Smart Office SQL Server DB to find the punch table.

Run on the office PC:   python discover_db.py

Connects to SQL Server (localhost / SmartOfficedb, Windows auth), lists every
table by row count, and for the likely punch tables prints columns + 3 sample
rows. Paste the WHOLE output back so we can wire up the real reader.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

SERVER = "localhost"
DATABASE = "SmartOfficedb"
KEYWORDS = ("log", "att", "punch", "device", "swipe", "inout", "trans", "daily", "muster", "raw")


def _ensure(pkg: str) -> None:
    try:
        importlib.import_module(pkg)
    except ImportError:
        print(f"Installing {pkg} (one-time)...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])


def main() -> None:
    _ensure("pyodbc")
    import pyodbc

    driver = [d for d in pyodbc.drivers() if "SQL Server" in d][-1]
    conn = pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={SERVER};DATABASE={DATABASE};Trusted_Connection=yes;",
        timeout=10,
    )
    cur = conn.cursor()

    cur.execute(
        """
        SELECT t.name, SUM(p.rows) AS rows
        FROM sys.tables t
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        GROUP BY t.name
        ORDER BY rows DESC
        """
    )
    table_rows = [(r[0], r[1]) for r in cur.fetchall()]
    print("TABLES BY ROW COUNT (top 25):")
    for name, n in table_rows[:25]:
        print(f"  {n:>9}  {name}")

    # Inspect tables that look like punch logs, plus the 5 biggest overall.
    likely = [name for name, _ in table_rows if any(k in name.lower() for k in KEYWORDS)]
    top = [name for name, _ in table_rows[:5]]
    for name in dict.fromkeys(likely + top):
        print(f"\n== {name} ==")
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?",
            name,
        )
        for col, dtype in cur.fetchall():
            print(f"   {col} : {dtype}")
        try:
            cur.execute(f"SELECT TOP 3 * FROM [{name}]")
            headers = [d[0] for d in cur.description]
            print("   sample rows:")
            for row in cur.fetchall():
                print("     ", dict(zip(headers, [str(v)[:32] for v in row])))
        except Exception as exc:  # noqa: BLE001
            print(f"   (sample error: {exc})")


if __name__ == "__main__":
    main()
