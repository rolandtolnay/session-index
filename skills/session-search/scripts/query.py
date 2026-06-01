#!/usr/bin/env python3
"""Query wrapper — resolves repo root via symlink, runs the CLI read-only query."""
from _bootstrap import repo_root

repo_root()

import argparse
import sys

from cli import add_query_arguments, cmd_query

parser = argparse.ArgumentParser(
    description="Run read-only SQL; --schema prints a curated fact-table reference and inspect-ref examples",
)
add_query_arguments(parser)
args = parser.parse_args()

if not args.sql and not args.schema:
    print('Usage: query.py "SELECT ..." [--json] [--limit N]   |   query.py --schema')
    sys.exit(1)

cmd_query(args)
