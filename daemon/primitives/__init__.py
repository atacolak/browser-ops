"""
primitives — standalone CDP building blocks.

Each module exposes async functions that operate on a CDPClient + session_id.
These are the raw browser automation atoms. They carry no state, no event
emission, no error envelopes — just CDP in, dict out.

Higher layers (backends, the RPC server) add state management, event logging,
and error wrapping on top.
"""

from . import nav, input, capture, tabs, files, replay, self_heal
from . import eval_mod

__all__ = ["nav", "input", "capture", "tabs", "eval_mod", "files", "replay", "self_heal"]
