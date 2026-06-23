# Fixture: `bad_pr` — a PR diff with a deliberate regression

The working tree is the **proposed** state of an open pull request; `pr.diff` is
the change under review. It carries a planted regression for the reviewer to
catch.

## Ground truth

- **The change (`pr.diff`):** flips `apply_restock` in `inventory.py` from
  `return stock + amount` to `return stock - amount`.
- **The regression:** a restock now *subtracts* stock. `test_inventory.py::
  test_restock_increases_stock` fails on the proposed state.
- **The correct verdict:** REQUEST_CHANGES, flagging the `apply_restock` line —
  the operator was inverted.
- **Reproducible round-trip:** the proposed tree is RED; reverse-applying
  `pr.diff` (`git apply --reverse pr.diff`) recovers the green pre-PR base. That
  proves the diff is exactly what introduced the regression.

Small and deterministic: a one-operator regression, isolated to one hunk.
