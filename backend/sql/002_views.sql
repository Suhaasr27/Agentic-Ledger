-- Read models. Everything the reports show is derived here, in SQL, from the ledger.
-- Because nothing is materialised, a report always reflects everything ingested so far,
-- and an event that was an orphan yesterday links itself the moment its lead arrives.

-- 1. Link every event to a lead (NULL lead_id = orphan).
--    * normal events: (workflow_id, phone_hash) -> lead
--    * disposition.override (no phone): via the lead of the same call's other, phone-linked events
CREATE VIEW v_event_lead AS
WITH direct AS (
    SELECT e.event_id, l.lead_id
    FROM events e
    JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
    WHERE e.event_type <> 'disposition.override'
),
call_lead AS (
    SELECT e.workflow_id, e.call_id, min(d.lead_id) AS lead_id
    FROM direct d
    JOIN events e USING (event_id)
    WHERE e.call_id IS NOT NULL
    GROUP BY e.workflow_id, e.call_id
)
SELECT e.event_id, e.workflow_id, e.call_id, e.event_type, e.occurred_at, e.payload,
       CASE WHEN e.event_type = 'disposition.override' THEN cl.lead_id ELSE d.lead_id END AS lead_id
FROM events e
LEFT JOIN direct d     ON d.event_id = e.event_id
LEFT JOIN call_lead cl ON cl.workflow_id = e.workflow_id AND cl.call_id = e.call_id;

-- 2. A call exists only if its call.initiated event is present and linked.
CREATE VIEW v_calls AS
SELECT workflow_id, call_id, lead_id,
       min(occurred_at) FILTER (WHERE event_type = 'call.initiated') AS initiated_at,
       bool_or(event_type = 'call.connected')                        AS connected,
       bool_or(event_type = 'call.telephony_failed')                 AS telephony_failed
FROM v_event_lead
WHERE lead_id IS NOT NULL AND call_id IS NOT NULL
GROUP BY workflow_id, call_id, lead_id
HAVING bool_or(event_type = 'call.initiated');

-- 3. Effective disposition per call: latest override, else latest set.
--    Codes missing from the catalogue are ignored (and counted in v_data_quality).
--    Ties on occurred_at break on event_id so the answer never depends on ingestion order.
CREATE VIEW v_call_disposition AS
SELECT DISTINCT ON (c.workflow_id, c.call_id, c.lead_id)
       c.workflow_id, c.call_id, c.lead_id, d.code, d.is_terminal, d.is_success
FROM v_calls c
JOIN v_event_lead el
  ON el.workflow_id = c.workflow_id AND el.call_id = c.call_id AND el.lead_id = c.lead_id
 AND el.event_type IN ('disposition.set', 'disposition.override')
JOIN dispositions d
  ON d.workflow_id = el.workflow_id AND d.code = el.payload ->> 'disposition_code'
ORDER BY c.workflow_id, c.call_id, c.lead_id,
         (el.event_type = 'disposition.override') DESC, el.occurred_at DESC, el.event_id DESC;

-- 4. One row per lead with call aggregates and its bucket.
CREATE VIEW v_lead_stats AS
WITH per_call AS (
    SELECT c.*, d.code, d.is_terminal, d.is_success
    FROM v_calls c
    LEFT JOIN v_call_disposition d USING (workflow_id, call_id, lead_id)
),
agg AS (
    SELECT lead_id,
           count(*)                                           AS total_calls,
           count(*) FILTER (WHERE connected)                  AS connected_calls,
           max(initiated_at)                                  AS last_call_at,
           bool_and(telephony_failed)                         AS all_telephony_failed,
           coalesce(bool_or(is_terminal), false)              AS any_terminal,
           coalesce(bool_or(is_success), false)               AS ever_success,
           (array_agg(code ORDER BY initiated_at DESC, call_id DESC)
                FILTER (WHERE code IS NOT NULL))[1]           AS current_disposition
    FROM per_call
    GROUP BY lead_id
)
SELECT l.lead_id, l.workflow_id, l.batch_id, l.external_ref, l.phone_last4,
       coalesce(a.total_calls, 0)     AS total_calls,
       coalesce(a.connected_calls, 0) AS connected_calls,
       a.last_call_at,
       a.current_disposition,
       coalesce(a.ever_success, false) AS ever_success,
       CASE WHEN a.total_calls IS NULL        THEN 'yet_to_reach_out'
            WHEN a.any_terminal               THEN 'completed'
            WHEN a.all_telephony_failed       THEN 'in_progress_telephony_failed'
            ELSE                                   'in_progress_dialed'
       END AS bucket
FROM leads l
LEFT JOIN agg a USING (lead_id);

CREATE VIEW v_batch_summary AS
SELECT workflow_id, batch_id,
       count(*)                                                        AS total_leads,
       count(*) FILTER (WHERE bucket = 'yet_to_reach_out')             AS yet_to_reach_out,
       count(*) FILTER (WHERE bucket = 'in_progress_telephony_failed') AS in_progress_telephony_failed,
       count(*) FILTER (WHERE bucket = 'in_progress_dialed')           AS in_progress_dialed,
       count(*) FILTER (WHERE bucket = 'completed')                    AS completed
FROM v_lead_stats
GROUP BY workflow_id, batch_id;

CREATE VIEW v_daily_overview AS
SELECT c.workflow_id,
       (c.initiated_at AT TIME ZONE 'Asia/Kolkata')::date               AS call_date,
       count(*)                                                         AS total_calls,
       count(*) FILTER (WHERE c.connected)                              AS connected_calls,
       count(DISTINCT c.lead_id) FILTER (WHERE d.is_success)            AS success_leads
FROM v_calls c
LEFT JOIN v_call_disposition d USING (workflow_id, call_id, lead_id)
GROUP BY c.workflow_id, (c.initiated_at AT TIME ZONE 'Asia/Kolkata')::date;

CREATE VIEW v_lead_report AS
SELECT workflow_id, external_ref, batch_id,
       '+91******' || phone_last4 AS phone_masked,
       bucket, total_calls, connected_calls,
       to_char(last_call_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD"T"HH24:MI:SS') || '+05:30' AS last_call_at,
       current_disposition, ever_success, lead_id
FROM v_lead_stats;

CREATE VIEW v_rejections AS
SELECT f.workflow_id, f.batch_id, r.row_number, r.reason, f.file_id
FROM lead_rejections r
JOIN ingested_files f USING (file_id);

CREATE VIEW v_rejections_by_reason AS
SELECT reason, count(*) AS n FROM lead_rejections GROUP BY reason;

-- Final-state counters. Duplicates are derived (accepted lines - rows actually in the ledger),
-- so they are exact however the drops were split.
CREATE VIEW v_data_quality AS
SELECT
    (SELECT coalesce(sum(records_total), 0)    FROM ingested_files WHERE kind = 'leads')  AS lead_rows_total,
    (SELECT coalesce(sum(records_rejected), 0) FROM ingested_files WHERE kind = 'leads')  AS lead_rows_rejected,
    (SELECT coalesce(sum(records_accepted), 0) FROM ingested_files WHERE kind = 'leads')
        - (SELECT count(*) FROM leads)                                                    AS duplicate_lead_rows_merged,
    (SELECT coalesce(sum(field_warnings), 0)   FROM ingested_files WHERE kind = 'leads')  AS field_warnings,
    (SELECT coalesce(sum(records_total), 0)    FROM ingested_files WHERE kind = 'events') AS events_total,
    (SELECT coalesce(sum(records_rejected), 0) FROM ingested_files WHERE kind = 'events') AS malformed_events,
    (SELECT coalesce(sum(records_accepted), 0) FROM ingested_files WHERE kind = 'events')
        - (SELECT count(*) FROM events)                                                   AS duplicate_events_dropped,
    (SELECT count(*) FROM v_event_lead WHERE lead_id IS NULL)                             AS orphan_events,
    (SELECT count(*)
       FROM v_event_lead el
      WHERE el.lead_id IS NOT NULL
        AND el.event_type IN ('disposition.set', 'disposition.override')
        AND NOT EXISTS (SELECT 1 FROM dispositions d
                         WHERE d.workflow_id = el.workflow_id
                           AND d.code = el.payload ->> 'disposition_code'))               AS unknown_disposition_events;

-- Lead profile with schema defaults applied AFTER merging, only to fields still null/absent.
-- (Nulls are never stored in attrs, so `defaults || attrs` is exactly "default unless a value exists".)
CREATE VIEW v_lead_profile AS
SELECT l.lead_id, l.workflow_id, l.batch_id, l.external_ref, l.phone_last4,
       coalesce((SELECT jsonb_object_agg(f ->> 'name', f -> 'default')
                   FROM jsonb_array_elements(w.schema -> 'fields') f
                  WHERE jsonb_typeof(f -> 'default') IS NOT NULL
                    AND jsonb_typeof(f -> 'default') <> 'null'), '{}'::jsonb) || l.attrs AS attrs
FROM leads l
JOIN workflows w USING (workflow_id);
