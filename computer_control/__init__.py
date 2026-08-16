"""computer_control - desktop control plugin for the dsh harness.

A self-contained plugin that lets an agent observe and operate a desktop:
screen capture, pointer and keyboard injection, accessibility-tree driven
semantic actions, all behind a safety gate (rules, confirmation flow,
emergency stop, idle standby).

Public entry points:
- ``python -m computer_control`` -> CLI (serve / check / list)
- ``computer_control.session.Session`` -> programmatic embedding
- ``computer_control.client.StdioClient`` -> talking to a running server
"""

from computer_control.version import __version__

__all__ = ["__version__"]
