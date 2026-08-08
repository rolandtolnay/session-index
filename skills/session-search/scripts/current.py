#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Current-session wrapper — resolves repo root via symlink, runs CLI current."""
from _bootstrap import import_cli

import argparse

add_current_arguments, cmd_current = import_cli("add_current_arguments", "cmd_current")

parser = argparse.ArgumentParser(description="Show the exact active runtime session from Session Index environment")
add_current_arguments(parser)
args = parser.parse_args()

cmd_current(args)
