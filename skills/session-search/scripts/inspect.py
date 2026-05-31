#!/usr/bin/env python3
"""Evidence Inspect wrapper — resolves repo root via symlink, runs CLI inspect."""
import argparse
import os
import sys

# Resolve symlink chain: scripts/ -> session-search/ -> skills/ -> repo_root
here = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
sys.path.insert(0, repo_root)

from cli import add_inspect_arguments, cmd_inspect

parser = argparse.ArgumentParser(description="Resolve an Inspection Reference into a JSON Evidence Packet")
add_inspect_arguments(parser)
args = parser.parse_args()

cmd_inspect(args)
