"""The orchestration spine (ARCHITECTURE.md §6).

One turn of the loop: guard-check the call ceiling, assemble context (message
history plus the ``core + loaded`` tool schemas from the registry), call the
model, dispatch each tool call it asks for, fold the typed results back in,
compact if the footprint crosses the threshold, then terminate on ``finish`` or
repeat.

The loop does only orchestration. It does not know what any individual tool
does, does not parse free text, and does not branch on tool identity — that
ignorance is deliberate, and it is what keeps the system out of the
fifty-conditional-dispatch anti-pattern the brief warns against.
"""
