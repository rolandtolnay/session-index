#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Evidence Find wrapper — resolves repo root via symlink, runs CLI find."""
from _bootstrap import import_cli

import argparse

add_find_arguments, cmd_find = import_cli("add_find_arguments", "cmd_find")

parser = argparse.ArgumentParser(
    description="Evidence Find: compact JSON candidates with refs, summaries, and match metadata; no evidence text or broad artifact inventories",
)
add_find_arguments(parser)
args = parser.parse_args()

cmd_find(args)
