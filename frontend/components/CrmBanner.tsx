"use client";

import type { Integration } from "@/lib/api";

type Props = { integration: Integration; approved: boolean; onRedeliver: () => void };

/** Shown on projects the CRM handed over: which record, and whether the approved DXF reached the CRM. */
export default function CrmBanner({ integration, approved, onRedeliver }: Props) {
  const last = integration.deliveries[integration.deliveries.length - 1];
  let state: { text: string; cls?: string; retry?: boolean };
  if (!integration.has_callback) state = { text: "The CRM collects the approved DXF from CadPilot." };
  else if (last?.ok) state = { text: `Revision ${last.revision} DXF delivered to the CRM · ${new Date(last.at).toLocaleString()}`, cls: "ok" };
  else if (last) state = { text: `Delivery of revision ${last.revision} failed${last.http_status ? ` (HTTP ${last.http_status})` : ""}: ${last.detail}`, cls: "err", retry: true };
  else if (approved) state = { text: "Sending the approved DXF to the CRM…" };
  else state = { text: "When you approve, the DXF is sent to the CRM automatically." };

  return (
    <div className="crm-banner" role="status">
      <span>
        From CRM record <span className="crm-id">{integration.external_id}</span>
      </span>
      <span className={`crm-state${state.cls ? ` ${state.cls}` : ""}`}>{state.text}</span>
      <span className="spacer" />
      {state.retry && (
        <button className="btn small" onClick={onRedeliver}>
          Send again
        </button>
      )}
      {integration.return_url && (
        <a className="btn small" href={integration.return_url}>
          ← Back to CRM
        </a>
      )}
    </div>
  );
}
