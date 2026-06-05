# tests/unit/ — Layer 1 (ARCHITECTURE.md §11.1)

Pure logic, mock model, **no network**. Registry ranking and loading, each
tool's behavior given known repo state, context compaction preserving plan state
while evicting raw payloads, retry/backoff on transient errors, and the schema
round-trip that proves one tool's output validates as another's input.

Target: dozens of tests, sub-second. Run with `make test-unit`.
