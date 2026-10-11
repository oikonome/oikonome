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
