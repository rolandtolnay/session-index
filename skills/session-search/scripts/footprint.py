#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Footprint wrapper — resolves repo root via symlink, runs CLI footprint audit."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import add_footprint_arguments, cmd_footprint

parser = argparse.ArgumentParser(
    description="Audit generated Session Index artifact disk usage and prune eligibility",
)
add_footprint_arguments(parser)
args = parser.parse_args()

cmd_footprint(args)
