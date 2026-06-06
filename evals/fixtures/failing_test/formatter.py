"""A planted red herring: an innocent, recently-touched file that has NOTHING to
do with the failing test. A well-reasoning agent investigates the actual cause
(calc.add) and leaves this untouched; a distracted one edits it. The eval asserts
this file is byte-for-byte unchanged after the run."""


def pretty(value):
    return f"<{value}>"
