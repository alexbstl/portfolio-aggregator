"""
Remove cash-only artifact snapshots from account_value_snapshots.

When a sync's positions fetch failed (before the preserve-on-error fix in
sync_once), the account's positions were wiped and a snapshot was written with
total_value ≈ cash only — a false plunge in the equity curve (Robinhood, with
~$1 cash, dropped to ~$0). This finds those live snapshots — ones whose total is
far below the account's normal value — and removes them. Where a reconstructed
row exists for the same day, the curve backfills automatically with the right
value; otherwise the line simply interpolates across the gap.

Dry-run by default. Pass --delete to actually remove rows.

    python clean_snapshots.py            # show what would be deleted
    python clean_snapshots.py --delete   # delete them

Detection: per account, flag live snapshots whose total_value is below
RATIO × (that account's max live total). Accounts whose max is under FLOOR
(genuinely cash-only / tiny) are skipped so real all-cash days aren't touched.
"""
import sys

from db import db

RATIO = 0.10      # flag live snapshots below this fraction of the account's normal max
FLOOR = 1000.0    # skip accounts whose max live total is under this


def main():
    do_delete = "--delete" in sys.argv
    total_flagged = 0

    with db() as conn:
        accounts = conn.execute(
            "SELECT id, name FROM accounts ORDER BY name"
        ).fetchall()

        for a in accounts:
            row = conn.execute(
                "SELECT MAX(total_value) AS m FROM account_value_snapshots "
                "WHERE account_id = ? AND source = 'live'",
                (a["id"],),
            ).fetchone()
            max_total = row["m"]
            if max_total is None or max_total < FLOOR:
                continue  # cash-only / tiny account — leave it alone

            threshold = RATIO * max_total
            bad = conn.execute(
                "SELECT snapshot_at, total_value, cash FROM account_value_snapshots "
                "WHERE account_id = ? AND source = 'live' AND total_value < ? "
                "ORDER BY snapshot_at",
                (a["id"], threshold),
            ).fetchall()
            if not bad:
                continue

            total_flagged += len(bad)
            name = a["name"] or a["id"]
            print(f"{name}: {len(bad)} artifact snapshot(s) "
                  f"(total < {threshold:,.0f}; normal max {max_total:,.0f})")
            print(f"    {bad[0]['snapshot_at']}  ->  {bad[-1]['snapshot_at']}")
            print(f"    e.g. total={bad[0]['total_value']:.2f} cash={bad[0]['cash'] or 0:.2f}")

            if do_delete:
                n = conn.execute(
                    "DELETE FROM account_value_snapshots "
                    "WHERE account_id = ? AND source = 'live' AND total_value < ?",
                    (a["id"], threshold),
                ).rowcount
                print(f"    deleted {n}")

    if total_flagged == 0:
        print("No artifact snapshots found.")
    elif not do_delete:
        print(f"\n{total_flagged} snapshot(s) would be deleted. "
              f"Re-run with --delete to remove them.")
    else:
        print(f"\nDeleted {total_flagged} snapshot(s). "
              f"Reconstructed rows (where present) backfill the gaps.")


if __name__ == "__main__":
    main()
