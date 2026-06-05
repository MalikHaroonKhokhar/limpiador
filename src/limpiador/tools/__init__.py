"""Tool subsystem — the capability layer (ARCHITECTURE.md §3-5, Layer 2).

Fifty-six tools across five coherent namespaces (``git.*``, ``github.*``,
``fs.*``, ``ast.*``, ``test.*``/``ci.*``), discovered and loaded dynamically
through the registry rather than handed to the model wholesale. The registry —
not the tool count — is the hard part: keeping fifty tools coherent so the model
*selects* the right one is the engineering this layer exists to do.
"""
