"""Ingest side of the archive service."""


def rows(raw):
    # 503s land here; the caller handles RETRY
    return raw.splitlines()
