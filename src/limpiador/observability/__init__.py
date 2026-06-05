"""Foundations / plumbing — the infrastructure layer (ARCHITECTURE.md §13, Layer 1).

The production scaffolding the brief enumerates, built from the start rather
than bolted on: structured tracing and token accounting, a typed ``ToolError``
hierarchy, exponential-backoff retries, and a token-bucket rate limiter. The
lowest layer; it imports from nothing above it.
"""
