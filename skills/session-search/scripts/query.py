#!/usr/bin/env python3
"""Query wrapper — resolves repo root via symlink, runs the CLI read-only query."""
import argparse
import os
import sys

# Resolve symlink chain: scripts/ -> session-search/ -> skills/ -> repo_root
here = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
sys.path.insert(0, repo_root)

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
