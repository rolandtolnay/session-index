#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Thin origin-resolution and synchronous Clean Transcript snapshot wrapper."""
from _bootstrap import import_cli

SnapshotArgumentParser, add_snapshot_arguments, cmd_snapshot = import_cli(
    "SnapshotArgumentParser", "add_snapshot_arguments", "cmd_snapshot",
)

parser = SnapshotArgumentParser(description="Resolve an exact commit origin before guarding its path, or capture its fresh Clean Transcript")
add_snapshot_arguments(parser)
cmd_snapshot(parser.parse_args())
