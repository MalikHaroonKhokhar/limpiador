"""A tiny calculator with one seeded defect: add subtracts instead of adding."""


def add(a, b):
    return a - b  # seeded bug: should be a + b


def multiply(a, b):
    return a * b
