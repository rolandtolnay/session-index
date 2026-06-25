#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Query wrapper — resolves repo root via symlink, runs the CLI read-only query."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import add_query_arguments, cmd_query

parser = argparse.ArgumentParser(
    description="Run read-only SQL; --schema prints a curated fact-table reference and inspect-ref examples",
)
add_query_arguments(parser)
args = parser.parse_args()
cmd_query(args)
