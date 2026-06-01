#!/usr/bin/env python3
"""Evidence Inspect wrapper — resolves repo root via symlink, runs CLI inspect."""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import add_inspect_arguments, cmd_inspect

parser = argparse.ArgumentParser(
    description="Evidence Inspect: resolve one ref into artifact metadata and scoped Evidence Snippets; session refs also work without --q",
)
add_inspect_arguments(parser)
args = parser.parse_args()

cmd_inspect(args)
