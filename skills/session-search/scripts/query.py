#!/usr/bin/env python3
"""Query wrapper — resolves repo root via symlink, runs the CLI read-only query."""
from _bootstrap import repo_root

repo_root()

import argparse
import sys

from cli import cmd_query

parser = argparse.ArgumentParser(description="Run a read-only SQL query against the session index")
parser.add_argument("sql", nargs="?", default=None, help="A single SELECT / WITH statement")
parser.add_argument("--json", action="store_true", help="Output rows as JSON")
parser.add_argument("--limit", type=int, default=50, help="Max rows (default 50, cap 1000)")
parser.add_argument("--schema", action="store_true", help="Print fact-table schema + examples and exit")
args = parser.parse_args()

if not args.sql and not args.schema:
    print('Usage: query.py "SELECT ..." [--json] [--limit N]   |   query.py --schema')
    sys.exit(1)

cmd_query(args)
