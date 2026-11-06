"""Canonical input discovery: media objects are opaque, including JSON files."""
import os
from pathlib import Path


def raw_json_files(root):
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name != "_media"]
        for name in names:
            if name.endswith(".json"):
                yield Path(parent) / name
