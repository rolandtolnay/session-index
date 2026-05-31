#!/usr/bin/env python3
"""Legacy excerpt wrapper.

Kept temporarily for compatibility; primary workflow is find -> inspect, with
query for aggregates/custom SQL. This wrapper is not documented by the skill.
"""
import argparse
import os
import sys

# Resolve symlink chain: scripts/ -> session-search/ -> skills/ -> repo_root
here = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
sys.path.insert(0, repo_root)

from cli import cmd_excerpt

parser = argparse.ArgumentParser(description="Extract transcript passages from specific sessions")
parser.add_argument("sessions", nargs="+", help="Session ID(s) or 8+ char prefix (max 3)")
parser.add_argument("--query", "-q", required=True, help="Keywords to focus extraction")
args = parser.parse_args()

cmd_excerpt(args)
