"""
worker.py — what a bank sync runs, in a process of its own: fetch from Plaid, then categorise.

BUDGETTRACKER_HOME must point at the person's private folder (see pipeline.py), so the
unchanged Mac pipeline reads and writes there instead of this project's data folder.
"""

import json

from src import enrich_transactions, plaid_sync


def main() -> None:
    result = plaid_sync.sync()
    enrich_transactions.enrich(statements_dir=plaid_sync.statements_dir())
    # The last line of output is what pipeline.run_pipeline reads
    print(json.dumps({"new_rows": result.new_rows, "complete": result.complete}))


if __name__ == "__main__":
    main()
