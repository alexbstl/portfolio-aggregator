"""
One-time backfill: reconstruct the equity curve for the period before the app
started taking snapshots, from SnapTrade transaction history.

Steps:
  1. Ingest all transaction activities into the `activities` table.
  2. For each account, replay activities forward (in today's split-adjusted
     share terms) and value holdings with historical closes, writing one
     reconstructed snapshot per trading day.
  3. Print a validation report per account.

Run a normal sync first (so current positions + cash are fresh) — reconstruction
is anchored to today's holdings. Re-runnable; reconstructed rows are replaced.

    python backfill_history.py
"""
from dotenv import load_dotenv

from db import db, init_db, reconstruct_account_history
from sync_once import sync_activities

load_dotenv()


def main():
    init_db()

    with db() as conn:
        accounts = [
            (r["id"], r["name"] or r["id"])
            for r in conn.execute("SELECT id, name FROM accounts ORDER BY name").fetchall()
        ]
        n_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]

    if not accounts:
        print("No accounts in the DB. Start the app (or run sync_once.py) first.")
        return
    if n_positions == 0:
        print("WARNING: no current positions in the DB. Reconstruction is anchored to")
        print("today's holdings — run a sync first or the curve will be wrong.\n")

    print(f"Ingesting activities for {len(accounts)} account(s)...")
    with db() as conn:
        total = sync_activities(conn, [a[0] for a in accounts])
    print(f"  {total} activities ingested.\n")

    print("Reconstructing equity curves...\n")
    for acct_id, name in accounts:
        with db() as conn:
            report = reconstruct_account_history(conn, acct_id)

        if report.get("status") == "no_activities":
            print(f"  {name}: no activities — skipped")
            continue

        print(f"  {name}")
        print(f"    {report['start_date']} -> {report['end_date']}  "
              f"({report['days_written']} days, {report['activities']} activities)")
        if report["unpriceable_symbols"]:
            print(f"    unpriceable (no value contribution): "
                  f"{', '.join(report['unpriceable_symbols'])}")
        if report["skipped_option_ma"]:
            print(f"    option/merger events skipped in share math: "
                  f"{report['skipped_option_ma']}")
        if report["holdings_residual"]:
            print(f"    ⚠ holdings residual (should be empty): {report['holdings_residual']}")
        else:
            print(f"    ✓ holdings reconcile to current positions")
        cr = report["cash_residual"]
        flag = "✓" if abs(cr) < 1.0 else "⚠"
        print(f"    {flag} cash residual: {cr}")
        print()

    print("Done. The reconstructed history now appears on the Performance chart (All range).")


if __name__ == "__main__":
    main()
