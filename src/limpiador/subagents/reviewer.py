"""The reviewer subagent — ``spawn_reviewer`` (ARCHITECTURE.md §9).

Isolated on three axes:

* **Isolated context** — starts fresh with only its task and the structured
  inputs it is given (the PR diff, the changed files); the parent's reasoning
  and tool history are invisible, so its judgment is not biased by the parent's
  hypotheses.
* **Scoped tool set** — constructed with a read-only registry (``fs.*`` reads,
  the ``ast.*`` namespace, ``github.get_pr``) and nothing that writes. The
  scoping is enforced at construction, not by convention — a reviewer that could
  commit would not be a reviewer.
* **Structured return** — runs its own loop to completion and hands back one
  typed ``ReviewResult`` (findings with severity/file/line/suggestion plus a
  verdict); its internal calls do not leak back to the parent.
"""
