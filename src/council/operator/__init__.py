"""Operator terminal: the ONLY place a broker write can be approved. Guards, Keychain, notifications.

These checks make accidental or agent-driven approval hard; they are not a security boundary on
their own. The real boundary is the separate, locked write keychain and the human at the prompt.
"""
