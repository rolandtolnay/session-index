#!/usr/bin/env python3
"""Evidence Inspect wrapper — resolves repo root via symlink, runs CLI inspect."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import add_inspect_arguments, cmd_inspect

parser = argparse.ArgumentParser(description="Resolve an Inspection Reference into a JSON Evidence Packet")
add_inspect_arguments(parser)
args = parser.parse_args()

cmd_inspect(args)
