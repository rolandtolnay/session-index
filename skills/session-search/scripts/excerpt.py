#!/usr/bin/env python3
"""Excerpt wrapper — resolves repo root via symlink, runs CLI excerpt."""
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
