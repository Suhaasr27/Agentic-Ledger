export type Bucket =
  | "yet_to_reach_out"
  | "in_progress_telephony_failed"
  | "in_progress_dialed"
  | "completed";

export interface Drop { drop: string; files: number; pending: number }
export interface IngestResult {
  drop: string; ingested: number; skipped: number; warnings: string[];
  files: { kind: string; workflow_id: string; file: string; status: "ingested" | "skipped";
           records_total: number; records_rejected: number; records_accepted: number }[];
}
export interface DataQuality {
  lead_rows_total: number; lead_rows_rejected: number; rejections_by_reason: Record<string, number>;
  duplicate_lead_rows_merged: number; field_warnings: number; events_total: number;
  malformed_events: number; duplicate_events_dropped: number; orphan_events: number;
  unknown_disposition_events: number;
}
export interface BatchRow {
  workflow_id: string; batch_id: string; total_leads: number; yet_to_reach_out: number;
  in_progress_telephony_failed: number; in_progress_dialed: number; completed: number;
}
export interface DailyRow {
  workflow_id: string; date: string; total_calls: number; connected_calls: number;
  connectivity_rate: string; success_leads: number;
}
export interface LeadRow {
  workflow_id: string; external_ref: string; batch_id: string; phone_masked: string; bucket: Bucket;
  total_calls: number; connected_calls: number; last_call_at: string | null;
  current_disposition: string | null; ever_success: boolean;
}
export interface LeadPage { total: number; limit: number; offset: number; rows: LeadRow[] }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { cache: "no-store", ...init });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const api = {
  drops: () => request<Drop[]>("/drops"),
  dataQuality: () => request<DataQuality>("/reports/data-quality"),
  batchSummary: () => request<BatchRow[]>("/reports/batch-summary"),
  dailyOverview: () => request<DailyRow[]>("/reports/daily-overview"),
  leads: (q: { workflow_id?: string; bucket?: string; limit: number; offset: number }) => {
    const p = new URLSearchParams();
    Object.entries(q).forEach(([k, v]) => v !== undefined && v !== "" && p.set(k, String(v)));
    return request<LeadPage>(`/reports/leads?${p}`);
  },
  ingest: (drop: string) => request<IngestResult>(`/drops/${encodeURIComponent(drop)}/ingest`, { method: "POST" }),
  publish: () => request<{ directory: string; rows: Record<string, number> }>("/reports/publish", { method: "POST" }),
};
