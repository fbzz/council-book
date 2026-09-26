"""Build tests/fixtures/sec/companyfacts_trimmed.json: SEC XBRL companyfacts (public domain) for the
issuers the stock-sleeve regression tests pin, trimmed to the concepts the rule reads (revenue, gross
profit, operating income, cost of revenue) and to facts FILED on or before a cut-off. Facts filed later cannot change anything visible
before the pinned decision dates (first report, and change (c) reads only filings up to the
compared quarter's own filing date), so the trimmed file reproduces the full file's features at
those dates; the test checks the pinned values.

Usage (needs the lab's bulk companyfacts.zip):
    uv run python tests/fixtures/make_sec_fixture.py [--zip <companyfacts.zip>]
"""

from __future__ import annotations

import argparse
import json
import os
import zipfile
from pathlib import Path

from council.stocks import pit

HERE = Path(__file__).resolve().parent

# CIK -> (label, filed cut-off)
PINNED = {46080: ("HAS", "2016-12-31"), 789019: ("MSFT", "2018-12-31"), 1413329: ("PM", "2018-12-31")}
CONCEPTS = sorted({c for f in ("revenue", "gross_profit", "operating_income") for c in pit.FLOW_CONCEPTS[f][pit.US_GAAP]}
                  | set(pit.COUNCIL_COST_CONCEPTS[pit.US_GAAP]))
DEFAULT_ZIP = Path(os.environ.get("COUNCIL_LAB_ROOT") or Path.home() / "Desktop" / "trading") / (
    "finetune/data/raw/sec/bulk/companyfacts.zip")


def trimmed(cf: dict, cutoff: str) -> dict:
    gaap = cf["facts"]["us-gaap"]
    out = {}
    for c in CONCEPTS:
        node = gaap.get(c)
        if not node:
            continue
        rows = [r for r in node["units"].get("USD", []) if r.get("filed", "9999") <= cutoff]
        if rows:
            out[c] = {"units": {"USD": rows}}
    return {"cik": cf["cik"], "entityName": cf.get("entityName"), "facts": {"us-gaap": out}}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    args = p.parse_args()
    doc = {"source": "SEC XBRL companyfacts bulk snapshot (public domain), trimmed", "companies": {}}
    with zipfile.ZipFile(args.zip) as z:
        for cik, (label, cutoff) in PINNED.items():
            cf = json.loads(z.read(f"CIK{cik:010d}.json"))
            doc["companies"][label] = {"cik": cik, "filed_cutoff": cutoff, "companyfacts": trimmed(cf, cutoff)}
    (HERE / "sec").mkdir(exist_ok=True)
    (HERE / "sec" / "companyfacts_trimmed.json").write_text(json.dumps(doc, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
