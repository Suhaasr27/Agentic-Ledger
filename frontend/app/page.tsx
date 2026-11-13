"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type BatchRow, type DailyRow, type DataQuality, type Drop, type LeadPage } from "@/lib/api";

const BUCKETS = [
  { key: "yet_to_reach_out", label: "Yet to reach out", color: "var(--b-yet)" },
  { key: "in_progress_telephony_failed", label: "Telephony failed", color: "var(--b-fail)" },
  { key: "in_progress_dialed", label: "Dialed", color: "var(--b-dial)" },
  { key: "completed", label: "Completed", color: "var(--b-done)" },
] as const;

const n = (v: number) => v.toLocaleString("en-IN");

export default function Dashboard() {
  const [drops, setDrops] = useState<Drop[]>([]);
  const [quality, setQuality] = useState<DataQuality | null>(null);
  const [batches, setBatches] = useState<BatchRow[]>([]);
  const [daily, setDaily] = useState<DailyRow[]>([]);
  const [leads, setLeads] = useState<LeadPage | null>(null);
  const [filter, setFilter] = useState({ workflow_id: "", bucket: "", offset: 0 });
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; error?: boolean } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [d, q, b, dl] = await Promise.all([api.drops(), api.dataQuality(), api.batchSummary(), api.dailyOverview()]);
      setDrops(d); setQuality(q); setBatches(b); setDaily(dl);
    } catch (e) {
      setMessage({ text: `Could not reach the API: ${(e as Error).message}`, error: true });
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    api.leads({ workflow_id: filter.workflow_id, bucket: filter.bucket, limit: 15, offset: filter.offset })
      .then(setLeads).catch(() => setLeads(null));
  }, [filter, quality]);

  async function run(label: string, action: () => Promise<string>) {
    setBusy(label); setMessage(null);
    try { setMessage({ text: await action() }); await refresh(); }
    catch (e) { setMessage({ text: (e as Error).message, error: true }); }
    finally { setBusy(null); }
  }

  const workflows = [...new Set(batches.map((b) => b.workflow_id))];

  return (
    <main>
      <div className="row spread">
        <div>
          <h1>Interaction Ledger</h1>
          <p className="sub">Reports are derived from an append-only ledger; ingesting a drop twice changes nothing.</p>
        </div>
        <button className="primary" disabled={!!busy} onClick={() =>
          run("publish", async () => {
            const r = await api.publish();
            return `Wrote 5 report files to ${r.directory} (${n(r.rows.lead_report)} leads).`;
          })}>
          {busy === "publish" ? "Publishing…" : "Publish report files"}
        </button>
      </div>

      {message && <p className={message.error ? "err" : "note"} role="status">{message.text}</p>}

      <div className="grid">
        <section className="panel">
          <h2>Drops</h2>
          {drops.length === 0 && <p className="note">No drops found in the data directory.</p>}
          <div className="grid">
            {drops.map((d) => (
              <div className="row spread" key={d.drop}>
                <div className="row">
                  <strong>{d.drop}</strong>
                  <span className="note">{d.files} files</span>
                  <span className={`pill ${d.pending ? "pending" : "done"}`}>
                    {d.pending ? `${d.pending} pending` : "fully ingested"}
                  </span>
                </div>
                <button disabled={!!busy} onClick={() =>
                  run(d.drop, async () => {
                    const r = await api.ingest(d.drop);
                    return `${r.drop}: ${r.ingested} files ingested, ${r.skipped} skipped as already ingested.`;
                  })}>
                  {busy === d.drop ? "Ingesting…" : d.pending ? "Ingest" : "Re-run (no-op)"}
                </button>
              </div>
            ))}
          </div>
        </section>

        {quality && (
          <section className="panel">
            <h2>Data quality</h2>
            <div className="tiles">
              <Tile label="Lead rows" v={quality.lead_rows_total} />
              <Tile label="Rejected rows" v={quality.lead_rows_rejected} flag />
              <Tile label="Duplicates merged" v={quality.duplicate_lead_rows_merged} />
              <Tile label="Field warnings" v={quality.field_warnings} />
              <Tile label="Event lines" v={quality.events_total} />
              <Tile label="Malformed" v={quality.malformed_events} flag />
              <Tile label="Duplicate events" v={quality.duplicate_events_dropped} />
              <Tile label="Orphan events" v={quality.orphan_events} flag />
              <Tile label="Unknown dispositions" v={quality.unknown_disposition_events} flag />
            </div>
            <p className="note">
              Rejections: {Object.entries(quality.rejections_by_reason).map(([k, v]) => `${k} ${n(v)}`).join(" · ") || "none"}
            </p>
          </section>
        )}

        <section className="panel">
          <h2>Batches</h2>
          <div className="legend">
            {BUCKETS.map((b) => <span key={b.key} style={{ ["--c" as string]: b.color }}>{b.label}</span>)}
          </div>
          <div className="scroll">
            <table>
              <thead><tr><th>Workflow</th><th>Batch</th><th className="num">Leads</th><th>Progress</th>
                {BUCKETS.map((b) => <th className="num" key={b.key}>{b.label}</th>)}</tr></thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={`${b.workflow_id}/${b.batch_id}`}>
                    <td>{b.workflow_id}</td><td>{b.batch_id}</td><td className="num">{n(b.total_leads)}</td>
                    <td><div className="bar" title={`${b.completed} completed of ${b.total_leads}`}>
                      {BUCKETS.map((k) => <i key={k.key} style={{ width: `${(b[k.key] / b.total_leads) * 100}%`, background: k.color }} />)}
                    </div></td>
                    {BUCKETS.map((k) => <td className="num" key={k.key}>{n(b[k.key])}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel">
          <h2>Daily connectivity</h2>
          {workflows.map((wf) => {
            const rows = daily.filter((d) => d.workflow_id === wf);
            const max = Math.max(...rows.map((r) => r.total_calls), 1);
            return (
              <div key={wf} style={{ marginBottom: 14 }}>
                <div className="row spread"><strong>{wf}</strong>
                  <span className="note">{rows.length} days · bar height = calls, tooltip = connectivity rate</span></div>
                <div className="spark">
                  {rows.map((r) => (
                    <i key={r.date} style={{ height: `${(r.total_calls / max) * 100}%` }}
                       title={`${r.date}: ${r.total_calls} calls, ${r.connected_calls} connected (${(Number(r.connectivity_rate) * 100).toFixed(1)}%), ${r.success_leads} success leads`} />
                  ))}
                </div>
              </div>
            );
          })}
        </section>

        <section className="panel">
          <div className="row spread">
            <h2>Leads</h2>
            <div className="row">
              <select value={filter.workflow_id} onChange={(e) => setFilter({ ...filter, workflow_id: e.target.value, offset: 0 })}>
                <option value="">All workflows</option>
                {workflows.map((w) => <option key={w}>{w}</option>)}
              </select>
              <select value={filter.bucket} onChange={(e) => setFilter({ ...filter, bucket: e.target.value, offset: 0 })}>
                <option value="">All buckets</option>
                {BUCKETS.map((b) => <option key={b.key} value={b.key}>{b.label}</option>)}
              </select>
            </div>
          </div>
          <div className="scroll">
            <table>
              <thead><tr><th>Reference</th><th>Batch</th><th>Phone</th><th>Bucket</th>
                <th className="num">Calls</th><th className="num">Connected</th><th>Last call (IST)</th><th>Disposition</th></tr></thead>
              <tbody>
                {leads?.rows.map((r) => (
                  <tr key={`${r.workflow_id}/${r.external_ref}/${r.batch_id}`}>
                    <td>{r.external_ref}</td><td>{r.batch_id}</td><td>{r.phone_masked}</td><td>{r.bucket}</td>
                    <td className="num">{r.total_calls}</td><td className="num">{r.connected_calls}</td>
                    <td>{r.last_call_at?.replace("T", " ").replace("+05:30", "") ?? "—"}</td>
                    <td>{r.current_disposition ?? "—"}{r.ever_success ? " ✓" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {leads && (
            <div className="row spread" style={{ marginTop: 10 }}>
              <span className="note">{n(leads.total)} leads · phones are masked; the full number is never stored in plaintext</span>
              <div className="row">
                <button disabled={filter.offset === 0} onClick={() => setFilter({ ...filter, offset: Math.max(0, filter.offset - leads.limit) })}>Prev</button>
                <button disabled={filter.offset + leads.limit >= leads.total} onClick={() => setFilter({ ...filter, offset: filter.offset + leads.limit })}>Next</button>
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function Tile({ label, v, flag }: { label: string; v: number; flag?: boolean }) {
  return <div className={`tile ${flag && v > 0 ? "flag" : ""}`}><b>{n(v)}</b><span>{label}</span></div>;
}
