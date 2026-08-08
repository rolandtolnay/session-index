#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Prune wrapper — resolves repo root via symlink, runs CLI audit-first prune."""
from _bootstrap import import_cli

import argparse

add_prune_arguments, cmd_prune = import_cli("add_prune_arguments", "cmd_prune")

parser = argparse.ArgumentParser(
    description="Dry-run audit and confirmed deletion for explicit low-value Session Index session IDs",
)
add_prune_arguments(parser)
args = parser.parse_args()

cmd_prune(args)
