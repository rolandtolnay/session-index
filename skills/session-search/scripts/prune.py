#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Prune wrapper — resolves repo root via symlink, runs CLI audit-first prune."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import cmd_prune

parser = argparse.ArgumentParser(
    description="Dry-run audit and confirmed deletion for explicit low-value Session Index session IDs",
)
parser.add_argument("sessions", nargs="+", help="Exact Canonical Session ID(s) to audit/prune")
parser.add_argument("--confirm", action="store_true", help="Delete eligible audited session IDs and owned generated artifacts")
parser.add_argument("--json", action="store_true", help="Output audit/deletion result as JSON")
args = parser.parse_args()

cmd_prune(args)
