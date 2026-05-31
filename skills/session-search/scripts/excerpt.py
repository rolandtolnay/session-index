#!/usr/bin/env python3
"""Legacy excerpt wrapper.

Kept temporarily for compatibility; primary workflow is find -> inspect, with
query for aggregates/custom SQL. This wrapper is not documented by the skill.
"""
from _bootstrap import repo_root

repo_root()

import argparse

from cli import cmd_excerpt

parser = argparse.ArgumentParser(description="Extract transcript passages from specific sessions")
parser.add_argument("sessions", nargs="+", help="Session ID(s) or 8+ char prefix (max 3)")
parser.add_argument("--query", "-q", required=True, help="Keywords to focus extraction")
args = parser.parse_args()

cmd_excerpt(args)
