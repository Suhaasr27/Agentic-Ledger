"""An independent, in-memory reference implementation of the spec (no database, no SQL).

It shares only the leaf value rules (``normalize_*``, ``validate_row``, header mapping) with the
pipeline; linking, dedupe, orphan handling, calls, dispositions, buckets and every report are
re-derived here with plain dicts, so agreement with the SQL read model is meaningful evidence.
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from ledger.leads import Accepted, validate_row
from ledger.normalize import IST, normalize_phone, parse_timestamp
from ledger.schema import Catalog


def run(drops: list[Path], catalog: Catalog) -> dict:
    disp = {(d.workflow_id, d.code): d for d in catalog.dispositions}
    leads: dict[tuple[str, str], dict] = {}
    rejections, q = [], Counter()

    # ---- leads, in ingest order
    for drop in drops:
        for wf_dir in sorted(p for p in (drop / "leads").glob("*") if p.is_dir()):
            schema = catalog.schemas[wf_dir.name]
            for path in sorted(wf_dir.glob("*.csv")):
                with path.open(newline="", encoding="utf-8-sig") as fh:
                    rows = csv.reader(fh)
                    header = next(rows)
                    mapping = schema.map_headers(header)
                    n = 0
                    for cells in rows:
                        if all(not c.strip() for c in cells):
                            continue
                        n += 1
                        q["lead_rows_total"] += 1
                        raw = {name: (cells[i] if i < len(cells) else "") for i, name in mapping.items()}
                        out = validate_row(schema, raw)
                        if not isinstance(out, Accepted):
                            q["lead_rows_rejected"] += 1
                            q[("reason", out.reason)] += 1
                            rejections.append((wf_dir.name, path.stem, n, out.reason))
                            continue
                        q["field_warnings"] += out.warnings
                        key = (wf_dir.name, out.values[schema.phone_field.name])
                        if key in leads:
                            q["duplicate_lead_rows_merged"] += 1
                            leads[key]["fields"].update(out.values)
                        else:
                            leads[key] = {"batch": path.stem, "fields": dict(out.values)}

    # ---- events, in ingest order
    seen, events = set(), []
    for drop in drops:
        for path in sorted((drop / "events").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                q["events_total"] += 1
                try:
                    e = json.loads(line)
                    assert isinstance(e, dict) and e.get("event_id")
                except (ValueError, AssertionError):
                    q["malformed_events"] += 1
                    continue
                if e["event_id"] in seen:
                    q["duplicate_events_dropped"] += 1
                    continue
                seen.add(e["event_id"])
                e["_ts"] = parse_timestamp(e["occurred_at"])
                e["_phone"] = normalize_phone(e.get("phone"))
                events.append(e)

    # ---- link (final state)
    call_lead: dict[tuple[str, str], tuple[str, str]] = {}
    for e in events:
        if e["event_type"] != "disposition.override" and (e["workflow_id"], e["_phone"]) in leads:
            call_lead.setdefault((e["workflow_id"], e["call_id"]), (e["workflow_id"], e["_phone"]))
    by_call = defaultdict(list)
    for e in events:
        if e["event_type"] == "disposition.override":
            lead_key = call_lead.get((e["workflow_id"], e.get("call_id")))
        else:
            lead_key = (e["workflow_id"], e["_phone"]) if (e["workflow_id"], e["_phone"]) in leads else None
        if lead_key is None:
            q["orphan_events"] += 1
            continue
        if e["event_type"].startswith("disposition.") and (e["workflow_id"], (e.get("payload") or {}).get("disposition_code")) not in disp:
            q["unknown_disposition_events"] += 1
        by_call[(lead_key, e["call_id"])].append(e)

    # ---- calls
    calls = []
    for (lead_key, call_id), evs in by_call.items():
        inits = [e["_ts"] for e in evs if e["event_type"] == "call.initiated"]
        if not inits:
            continue
        wf = lead_key[0]
        valid = [e for e in evs if e["event_type"].startswith("disposition.")
                 and (wf, (e.get("payload") or {}).get("disposition_code")) in disp]
        overrides = sorted((e for e in valid if e["event_type"] == "disposition.override"),
                           key=lambda e: (e["_ts"], e["event_id"]))
        sets = sorted((e for e in valid if e["event_type"] == "disposition.set"),
                      key=lambda e: (e["_ts"], e["event_id"]))
        chosen = overrides[-1] if overrides else (sets[-1] if sets else None)
        calls.append({
            "lead": lead_key, "call_id": call_id, "initiated": min(inits),
            "connected": any(e["event_type"] == "call.connected" for e in evs),
            "failed": any(e["event_type"] == "call.telephony_failed" for e in evs),
            "disp": disp[(wf, chosen["payload"]["disposition_code"])] if chosen else None,
        })

    per_lead = defaultdict(list)
    for c in calls:
        per_lead[c["lead"]].append(c)

    # ---- reports (python sort == byte order for these ASCII keys)
    ref_name = {wf: s.reference_field.name for wf, s in catalog.schemas.items()}
    lead_rows, batch = [], defaultdict(Counter)
    for key, lead in leads.items():
        cs = per_lead.get(key, [])
        if not cs:
            bucket = "yet_to_reach_out"
        elif any(c["disp"] and c["disp"].is_terminal for c in cs):
            bucket = "completed"
        elif all(c["failed"] for c in cs):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        last = max(cs, key=lambda c: (c["initiated"], c["call_id"]), default=None)
        with_disp = [c for c in cs if c["disp"]]
        cur = max(with_disp, key=lambda c: (c["initiated"], c["call_id"]))["disp"].code if with_disp else ""
        batch[(key[0], lead["batch"])][bucket] += 1
        batch[(key[0], lead["batch"])]["total"] += 1
        lead_rows.append([
            key[0], lead["fields"][ref_name[key[0]]], lead["batch"], "+91******" + key[1][-4:], bucket,
            str(len(cs)), str(sum(c["connected"] for c in cs)),
            last["initiated"].astimezone(IST).strftime("%Y-%m-%dT%H:%M:%S+05:30") if last else "",
            cur, "true" if any(c["disp"] and c["disp"].is_success for c in cs) else "false"])
    lead_rows.sort(key=lambda r: (r[0], r[1], r[2]))

    daily = defaultdict(lambda: {"t": 0, "c": 0, "s": set()})
    for c in calls:
        d = daily[(c["lead"][0], c["initiated"].astimezone(IST).date().isoformat())]
        d["t"] += 1
        d["c"] += c["connected"]
        if c["disp"] and c["disp"].is_success:
            d["s"].add(c["lead"])

    quality = {
        "lead_rows_total": q["lead_rows_total"], "lead_rows_rejected": q["lead_rows_rejected"],
        "rejections_by_reason": {k[1]: v for k, v in sorted(q.items(), key=str) if isinstance(k, tuple)},
        "duplicate_lead_rows_merged": q["duplicate_lead_rows_merged"], "field_warnings": q["field_warnings"],
        "events_total": q["events_total"], "malformed_events": q["malformed_events"],
        "duplicate_events_dropped": q["duplicate_events_dropped"], "orphan_events": q["orphan_events"],
        "unknown_disposition_events": q["unknown_disposition_events"],
    }
    return {
        "batch_summary.csv": [[w, b, str(c["total"]), str(c["yet_to_reach_out"]),
                               str(c["in_progress_telephony_failed"]), str(c["in_progress_dialed"]),
                               str(c["completed"])] for (w, b), c in sorted(batch.items())],
        "daily_overview.csv": [[w, d, str(v["t"]), str(v["c"]), f"{v['c'] / v['t']:.4f}", str(len(v["s"]))]
                               for (w, d), v in sorted(daily.items())],
        "lead_report.csv": lead_rows,
        "rejections.csv": [[w, b, str(n), r] for w, b, n, r in sorted(rejections)],
        "data_quality.json": quality,
    }
