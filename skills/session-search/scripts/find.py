#!/usr/bin/env python3
"""Evidence Find wrapper — resolves repo root via symlink, runs CLI find."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import add_find_arguments, cmd_find

parser = argparse.ArgumentParser(description="Find compact JSON evidence candidates")
add_find_arguments(parser)
args = parser.parse_args()

cmd_find(args)
