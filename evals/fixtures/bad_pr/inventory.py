"""Stock tracking. The working tree is the *proposed* state of an open PR whose
change is captured in pr.diff — and that change regressed `apply_restock`."""


def apply_restock(stock, amount):
    # Regression proposed by the PR: a restock must ADD to stock, not subtract.
    return stock - amount


def is_in_stock(stock):
    return stock > 0
