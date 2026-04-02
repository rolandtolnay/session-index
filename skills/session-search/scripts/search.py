#!/usr/bin/env python3
"""Search wrapper — resolves repo root via symlink, runs CLI search."""
import argparse
import os
import sys

# Resolve symlink chain: scripts/ -> session-search/ -> skills/ -> repo_root
here = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
sys.path.insert(0, repo_root)

from cli import cmd_search

parser = argparse.ArgumentParser(description="Search past Claude Code conversations")
parser.add_argument("query", nargs="*", help="Search terms")
parser.add_argument("--project", "-p", help="Filter by project name (prefix match)")
parser.add_argument("--since", help="Only sessions from this date (YYYY-MM-DD)")
parser.add_argument("--until", help="Only sessions before this date (YYYY-MM-DD)")
parser.add_argument("--excerpt", "-e", action="store_true", help="Include transcript excerpts")
parser.add_argument("--any", action="store_true", default=True, help="Match ANY term (OR) — default for skill")
parser.add_argument("--no-any", dest="any", action="store_false", help="Match ALL terms (AND)")
parser.add_argument("--limit", type=int, default=20)
args = parser.parse_args()

# Join positional args into query string, or None if empty
args.query = " ".join(args.query) if args.query else None

if not args.query and not args.project and not args.since and not args.until:
    print("Usage: search.py [query] [--project NAME] [--since DATE] [--until DATE]")
    sys.exit(1)

cmd_search(args)
