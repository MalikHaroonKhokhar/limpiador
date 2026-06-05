"""Dev-mock process entrypoint (ARCHITECTURE.md §12, .clauderules §5 & §7).

``make dev-mock`` runs limpiador as a *real* process, but in mock mode. By design
(HAR-8) production never imports the mock, so ``LIMPIADOR_LLM=mock`` is
unavailable in a bare ``python -m limpiador.cli`` process — the mock becomes
selectable only when the test-support layer is loaded, and only then.

This tiny bootstrap is that seam for development. Running ``python -m support``
first imports the ``support`` package (its ``__init__`` registers the
deterministic mock via :mod:`support.mock_llm`), then hands off to the *real*
CLI, unchanged. It lives here in the test-support layer — never in ``src`` — so
the rule "src never names the mock" holds while ``make dev-mock`` still works.
"""

from __future__ import annotations

import sys

from limpiador.cli import main

# Importing this module's package (support/__init__) already registered the mock
# adapter as an import side effect, so build_adapter() can now resolve mock mode.
if __name__ == "__main__":
    sys.exit(main())
