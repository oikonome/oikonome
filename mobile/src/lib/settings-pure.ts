// Settings-screen rules that read the settings view, pure so the logic
// tests can hold them.

export type LlmBackendSeed = {
  id: string; name: string; url: string; model: string;
  vision_model?: string; extra_body?: string | null;
  api_key_set?: boolean; extra_body_set?: boolean;
};

/** The named backends, or — for a household still on the single-endpoint
 *  settings — that endpoint as one synthetic "legacy" row. The row carries
 *  every field the endpoint has, the vision model included: the first
 *  save folds the legacy keys into this row and drops them, so a field
 *  the row leaves out is deleted. The extra JSON is masked like a key —
 *  "" keeps what is stored. */
export function llmBackendsSeed(d: Record<string, unknown>): LlmBackendSeed[] {
  const rows = (d.llm_backends as LlmBackendSeed[] | null) ?? [];
  if (rows.length) return rows;
  if (!d.llm_url) return [];
  return [{ id: "legacy", name: "Custom endpoint", url: String(d.llm_url),
            model: String(d.llm_model ?? ""),
            vision_model: String(d.llm_vision_model ?? ""),
            extra_body: "", extra_body_set: !!d.llm_extra_body_set,
            api_key_set: !!d.llm_api_key_set }];
}

/** The Enrich cap a save stored. The server clamps the typed number to
 *  the instance's ceiling, so the reply's value is the truth; the typed
 *  one stands only when the reply does not carry it. */
export function storedEnrichCap(
  reply: Record<string, unknown> | null | undefined,
  typed: number,
): number {
  const v = reply?.plaid_enrich_cap;
  return typeof v === "number" && Number.isFinite(v) ? v : typed;
}

// ---- Investment fees & basis (the web's webapp/src/investsettings.ts,
// which the logic tests hold this copy to). The server stores a fee as a
// fraction because the estimate multiplies by it; people type a
// percentage, so the form converts at its edge and nowhere else.

export type FeeRow = { inst: string; pct: string };
export type BasisRow = { mask: string; amount: string };
export type InvestForm = { fees: FeeRow[]; exempt: string; basis: BasisRow[] };

export type InvestBody = {
  advisory_fees: Record<string, number>;
  advisory_fee_exempt_masks: string[];
  investment_basis_override: Record<string, number>;
};

/** The form as the settings view describes it. */
export function investFormOf(view: object | null | undefined): InvestForm {
  const d = view as { advisory_fees?: unknown; advisory_fee_exempt_masks?: unknown;
                      investment_basis_override?: unknown } | null | undefined;
  const fees = (d?.advisory_fees ?? {}) as Record<string, unknown>;
  const basis = (d?.investment_basis_override ?? {}) as Record<string, unknown>;
  const masks = (d?.advisory_fee_exempt_masks ?? []) as unknown[];
  return {
    fees: Object.entries(fees && typeof fees === "object" ? fees : {})
      .map(([inst, r]) => ({
        inst,
        // a fraction to six decimal places of a percent, so 0.0025 reads
        // 0.25 rather than 0.25000000000000006
        pct: String(Math.round(Number(r) * 1e8) / 1e6),
      })),
    exempt: (Array.isArray(masks) ? masks : []).map(String).join(", "),
    basis: Object.entries(basis && typeof basis === "object" ? basis : {})
      .map(([mask, v]) => ({ mask, amount: String(v) })),
  };
}

// The tail of the account number as the aggregator reports it: Plaid's
// masks are a few characters and some banks put letters in them. Upper
// case is how the feeds report them and how the server stores them.
const MASK = /^[A-Za-z0-9]{2,8}$/;

/** What Save posts, or the first thing wrong with the form. A row left
 *  entirely blank is an unused line, not a mistake. */
export function investBody(f: InvestForm,
): { ok: true; body: InvestBody } | { ok: false; error: string } {
  const advisory_fees: Record<string, number> = {};
  for (const r of f.fees) {
    const inst = r.inst.trim().toLowerCase();
    const pct = r.pct.trim().replace(/%$/, "").trim();
    if (!inst && !pct) continue;
    // a blank name would match every institution and charge the fee on
    // the whole portfolio
    if (!inst) return { ok: false, error: "Each fee needs an institution name." };
    const n = Number(pct);
    if (pct === "" || !Number.isFinite(n) || n < 0 || n > 100)
      return { ok: false,
               error: `The fee for ${inst} must be a percentage from 0 to 100.` };
    advisory_fees[inst] = Math.round(n * 1e6) / 1e8;
  }
  const advisory_fee_exempt_masks: string[] = [];
  for (const t of f.exempt.split(/[\s,;]+/)) {
    if (!t) continue;
    if (!MASK.test(t))
      return { ok: false,
               error: "Fee-free accounts are the last 2 to 8 letters or digits "
                      + "of each account, separated by commas." };
    const m = t.toUpperCase();
    if (!advisory_fee_exempt_masks.includes(m)) advisory_fee_exempt_masks.push(m);
  }
  const investment_basis_override: Record<string, number> = {};
  for (const r of f.basis) {
    const mask = r.mask.trim().toUpperCase();
    const amt = r.amount.trim().replace(/[$,]/g, "");
    if (!mask && !amt) continue;
    if (!MASK.test(mask))
      return { ok: false,
               error: "Each money-in override needs the account's last 2 to 8 "
                      + "letters or digits." };
    const n = Number(amt);
    if (amt === "" || !Number.isFinite(n) || n < 0)
      return { ok: false,
               error: `Money in for account ${mask} must be a dollar amount `
                      + "of 0 or more." };
    investment_basis_override[mask] = Math.round(n * 100) / 100;
  }
  return { ok: true,
           body: { advisory_fees, advisory_fee_exempt_masks,
                   investment_basis_override } };
}

// One section as compared for "did it change": the order rows were typed
// in is not a change the server would see differently.
function sectionKey(v: Record<string, number> | string[]): string {
  return JSON.stringify(Array.isArray(v) ? [...v].sort()
    : Object.entries(v).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));
}

/** The sections exactly as the settings view stored them, kept beside a
 *  draft so Save can name what it started from. */
export type InvestSeen = Record<keyof InvestBody, unknown>;

export function investSeenOf(view: object | null | undefined): InvestSeen {
  const d = (view ?? {}) as Record<string, unknown>;
  return { advisory_fees: d.advisory_fees ?? null,
           advisory_fee_exempt_masks: d.advisory_fee_exempt_masks ?? null,
           investment_basis_override: d.investment_basis_override ?? null };
}

export type InvestSave = Partial<InvestBody>
  & Partial<Record<`${keyof InvestBody}_base`, unknown>>;

/** What Save posts when the form was opened on `base`: only the sections
 *  that differ from it. Each section replaces its stored key whole, so
 *  posting one nobody touched would put back what this form loaded and
 *  drop anything another device saved since. An edited section carries
 *  the stored value it started from (`seen`) as `<key>_base`, so the
 *  server can refuse it when another device changed that same section in
 *  the meantime. An empty body means there is nothing to save. */
export function investChanges(base: InvestForm, f: InvestForm,
                              seen?: InvestSeen,
): { ok: true; body: InvestSave } | { ok: false; error: string } {
  const r = investBody(f);
  if (!r.ok) return r;
  const b = investBody(base);
  const body: Record<string, unknown> = {};
  for (const k of ["advisory_fees", "advisory_fee_exempt_masks",
                   "investment_basis_override"] as const) {
    // a base this form cannot read back is treated as changed everywhere,
    // so what the person sees is what gets stored
    if (!b.ok || sectionKey(b.body[k]) !== sectionKey(r.body[k])) {
      body[k] = r.body[k];
      if (seen) body[`${k}_base`] = seen[k];
    }
  }
  return { ok: true, body: body as InvestSave };
}

/** The server's answer when another device changed an edited section
 *  since this form loaded it. */
export const INVEST_STALE_STATUS = 409;
