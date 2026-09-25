"""Data layer: provider fetchers and parsers that return canonical, lookahead-safe frames.

Every fetcher is a pure function of (payload, now): bars are returned only once complete (or, for
Tiingo, once available), and nothing here ever logs a token, a key or a request URL."""
