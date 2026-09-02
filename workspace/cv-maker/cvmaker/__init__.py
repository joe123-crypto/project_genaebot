"""Standalone CV maker engine.

The paid "CV Maker" WhatsApp service: an order store, an LLM interview (Agent B
owns cvmaker/interview.py), a PDF renderer, and delivery of the finished CV back
to the individual who asked for it. See CONTRACT.md for the pinned interfaces.

Nothing is imported eagerly here: the CLI pulls in the module it needs, so a
module that is still being written cannot break the ones that are done.
"""

from __future__ import annotations


class CvMakerError(RuntimeError):
  """A failure that is safe to show a public user.

  The CLI turns this into {"ok": false, "error": ...}. Messages must never carry
  a traceback, a file path, a phone number, an email address, or CV content.
  """


__all__ = ["CvMakerError"]
