"""LLM access: the Ollama gateway, a deterministic stub, the prompt registry and sanitization.

Nothing in this package raises on a bad model reply: every call returns an `LLMResult` whose
`call.status` says what happened (ok / parse_fail / timeout / transport)."""
