#!/usr/bin/env python3
"""Current-session wrapper — resolves repo root via symlink, runs CLI current."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import cmd_current

parser = argparse.ArgumentParser(description="Show the exact active runtime session from Session Index environment")
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
