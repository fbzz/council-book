"""Public record: allow-listed models, redaction, leak scan, commit-reveal, journal layout, git.

Nothing in this package may import the broker writer, the Keychain or any private ledger object
other than the read-only contracts in `council.models` (a boundary test enforces it).
"""
