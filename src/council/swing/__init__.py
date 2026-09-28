"""The swing book (design swing-book.md rev 2): agent-driven stock swing trades, long and 1x CFD
short, as ledger objects beside the core book. Nothing here trades while
`council.invariants.SWING_BOOK_LIVE` is False. Keep this module import-free: `council.policy`
imports `council.swing.policy`."""
