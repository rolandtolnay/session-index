#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["rapidfuzz>=3.0"]
# ///
"""Interactive session manager wrapper; delegates to the CLI."""
from _bootstrap import import_cli

import argparse

(cmd_manage,) = import_cli("cmd_manage")

parser = argparse.ArgumentParser(
    description="Full-screen session browser: arrows select, Tab switches all/hidden, h hides/unhides, d confirms deletion, q quits. Left/right page; PgUp/PgDn scroll preview. Requires an interactive terminal; raw transcripts remain and can be re-indexed.",
)
cmd_manage(parser.parse_args())
