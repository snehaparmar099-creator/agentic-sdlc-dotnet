"""Agentic SDLC orchestrator.

Agents propose; the engine decides. Agents never touch the filesystem, the
network beyond their LLM provider, or the run state directly: they return
structured results that the engine validates against policy, applies inside a
git-backed workspace, verifies through exit gates, and records in a
hash-chained audit log.
"""

__version__ = "1.0.0"
