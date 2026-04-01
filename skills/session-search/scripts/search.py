#!/usr/bin/env python3
"""Search wrapper — resolves repo root via symlink, runs CLI search."""
import argparse
import os
import sys

# Resolve symlink chain: scripts/ -> session-search/ -> skills/ -> repo_root
here = os.path.dirname(os.path.realpath(__file__))
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
sys.path.insert(0, repo_root)

from cli import cmd_search

query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
if not query:
    print("Usage: search.py <query>")
    sys.exit(1)

ns = argparse.Namespace(query=query, limit=20)
cmd_search(ns)
