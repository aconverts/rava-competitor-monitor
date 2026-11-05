#!/usr/bin/env python3
"""Fetch our own catalog (ravaperfumes.com) so it can be compared with competitors.
Saves data/rava/catalog.json. Matching is done in gap_report.py."""
import json
from pathlib import Path

import monitor

BASE = "https://ravaperfumes.com"
out = []
for p in monitor.paged_json(f"{BASE}/products.json", "products", max_pages=80):
    out.append({
        "title": (p.get("title") or "").strip(),
        "handle": p.get("handle"),
        "type": p.get("product_type") or "",
        "vendor": p.get("vendor") or "",
        "tags": p.get("tags") if isinstance(p.get("tags"), list) else
                [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()],
        "prices": sorted({monitor.money(v.get("price")) for v in p.get("variants") or []} - {None}),
    })
path = Path(monitor.DATA) / "rava" / "catalog.json"
monitor.save_json(path, out)
print(f"RAVA catalog: {len(out)} products saved to {path}")
