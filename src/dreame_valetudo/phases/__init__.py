"""Rooting phases.

Each phase takes a Context and issues its external-command sequence through the runner; the
brick-critical gates (download verification, the config cross-check, the OKAY-gated flash) are
preserved exactly and proven by the tests under tests/python/.
"""
