"""A tiny data pipeline. The seeded defect lives HERE, in `normalize`."""


def normalize(rows):
    # Seeded bug: `rows[1:]` silently drops the first row. It should be `rows`.
    return [row.strip() for row in rows[1:]]
