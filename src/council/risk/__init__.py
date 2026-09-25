"""Risk engine: deterministic code that enforces every number in `policy/risk.yaml` and
`policy/costs.yaml`. Prompts may explain these rules; only this package enforces them.

Modules: config (typed policy views), exposure (broker P&L -> signed line weights), nav (lifetime
peak), killswitch (NORMAL/WARN/HALTED/FLAT), authority (council bands), projection (signed box + L1),
stops (catastrophe stops), costs (floors, carry, SR_be), churn (deadband, min hold, event and
anti-chase blocks), checks (R1..R21 rows) and engine (the whole pipeline)."""
