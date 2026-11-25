import { errText, type ReceiptTwin } from "../api/client";
import { useSlipAnswer } from "../api/waiting";

// Which slip a receipt is (specs/receipts.md, "A receipt is one slip"):
// the other copies of it, and receipts read exactly alike that are not —
// for those the person answers "Same receipt" or "Two purchases", once.
// The server decides which copy counts; this only says so.

const fmtDay = (d: string | null) => {
  const m = d ? /^(\d{4})-(\d{2})-(\d{2})/.exec(d) : null;
  return m ? `${m[2]}/${m[3]}/${m[1].slice(2)}` : (d ?? "");
};

/** Where the other receipt is: "the receipt on Corner Cafe, 10/31/26,
 *  $4.50", or the waiting one with what it read. */
export function twinLabel(t: ReceiptTwin): string {
  const bits = [t.payee, fmtDay(t.date),
                t.amount != null ? `$${t.amount.toFixed(2)}` : null]
    .filter(Boolean).join(", ");
  if (t.waiting) return `the waiting receipt${bits ? ` (${bits})` : ""}`;
  if (t.off_ledger) {
    return "the receipt on a charge no longer on the ledger"
      + (bits ? ` (${bits})` : "");
  }
  return `the receipt on ${bits || "another charge"}`;
}

/** What counts while the person has not answered: this receipt's own
 *  lines (`counts`, from the server) and the look-alike's. */
export function suspectedNote(t: ReceiptTwin, counts: boolean): string {
  if (counts && t.counted) return "both are counted until you say which";
  if (counts) {
    return "only this one is counted (that one's charge is no longer on "
      + "the ledger); say whether they are one purchase";
  }
  if (t.counted) {
    return "only that one is counted; say whether they are one purchase";
  }
  return "neither is counted; say whether they are one purchase";
}

export default function SlipNote({ id, counts, twins, suspected, mayAnswer }: {
  id: string; counts: boolean; twins: ReceiptTwin[];
  suspected: ReceiptTwin[]; mayAnswer: boolean;
}) {
  const answer = useSlipAnswer();
  if (!twins.length && !suspected.length) return null;
  const counted = twins.find((t) => t.counted);
  return (
    <div className="mut" style={{ fontSize: 12, marginTop: ".35rem" }}>
      {twins.length > 0 && (
        <div>
          {counted
            ? <>the same receipt as {twinLabel(counted)} — its lines are
                counted there, once</>
            : <>the same receipt as {twinLabel(twins[0])} — its lines are
                counted here, once</>}
          {mayAnswer && (
            <button className="btn" style={{ marginLeft: ".4rem" }}
              disabled={answer.isPending}
              onClick={() => answer.mutate({
                id, other: (counted ?? twins[0]).receipt_id, same: false })}>
              Two purchases</button>)}
        </div>)}
      {suspected.map((t) => (
        <div key={t.receipt_id} style={{ marginTop: ".2rem" }}>
          looks like the same receipt as {twinLabel(t)} — {suspectedNote(t, counts)}
          {mayAnswer && (<>
            <button className="btn" style={{ marginLeft: ".4rem" }}
              disabled={answer.isPending}
              onClick={() => answer.mutate({ id, other: t.receipt_id,
                                             same: true })}>
              Same receipt</button>
            <button className="btn" style={{ marginLeft: ".3rem" }}
              disabled={answer.isPending}
              onClick={() => answer.mutate({ id, other: t.receipt_id,
                                             same: false })}>
              Two purchases</button>
          </>)}
        </div>))}
      {answer.isError && (
        <div style={{ color: "var(--red)" }}>{errText(answer.error)}</div>)}
    </div>
  );
}
