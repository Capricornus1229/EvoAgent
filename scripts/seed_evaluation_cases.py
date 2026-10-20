"""Explicitly import the seven builtin smoke cases into SQLite or PostgreSQL.

This command creates only the selected store. It does not start the review
service, configure a model, load .env, or run an evaluation.
"""
import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evoagent.evolution import seed_builtin_evaluation_cases  # noqa: E402
from evoagent.postgres_store import create_store  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--builtin", action="store_true", required=True,
        help="Import builtin smoke fixtures; they do not establish real-PR accuracy.",
    )
    parser.add_argument(
        "--db-path", default=os.getenv("EVOAGENT_DB_PATH", "evoagent.db"),
        help="SQLite destination (default: EVOAGENT_DB_PATH or evoagent.db).",
    )
    parser.add_argument(
        "--database-url", default=os.getenv("EVOAGENT_DATABASE_URL", ""),
        help="PostgreSQL destination; takes precedence over --db-path.",
    )
    args = parser.parse_args(argv)
    if args.database_url and not args.database_url.startswith(("postgres://", "postgresql://")):
        parser.error("--database-url must be a PostgreSQL URL")
    store = create_store(args.database_url, args.db_path)
    cases = seed_builtin_evaluation_cases(store)
    print(json.dumps({
        "corpus": "builtin-smoke", "cases": len(cases),
        "validation_cases": sum(case["split"] == "validation" for case in cases),
        "holdout_cases": sum(case["split"] == "holdout" for case in cases),
        "backend": "postgresql" if args.database_url else "sqlite",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
