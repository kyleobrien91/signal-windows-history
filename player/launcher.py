#!/usr/bin/env python3
"""Player CLI bootstrap.

Resolves the packaged web-asset directory before delegating to the player server.
"""

from importlib.resources import files


def main() -> None:
    from player import server

    server.WEB_DIR = str(files("web"))
    server.main()


__all__ = ["main"]
