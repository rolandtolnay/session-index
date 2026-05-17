#!/usr/bin/env python3
"""Current-session wrapper — resolves repo root via symlink, runs CLI current."""
import argparse
import os
import sys

# Resolve symlink chain: scripts/ -> session-search/ -> skills/ -> repo_root
here = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
sys.path.insert(0, repo_root)

from cli import cmd_current

parser = argparse.ArgumentParser(description="Show the active runtime session")
output = parser.add_mutually_exclusive_group()
output.add_argument(
    "--path",
    action="store_true",
    help="Print the deterministic clean transcript path; warn if it does not exist yet",
)
output.add_argument("--native", action="store_true", help="Print the provider-native session ID")
output.add_argument("--json", action="store_true", help="Print full current-session metadata as JSON")
args = parser.parse_args()

cmd_current(args)
