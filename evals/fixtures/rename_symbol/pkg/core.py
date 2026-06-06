"""Defines `compute`, used here and in pkg/consumer.py — a symbol that must be
renamed across more than one file for the rename to be correct."""

BASE = 10


def compute(value):
    return value + BASE


def run():
    return compute(5)
