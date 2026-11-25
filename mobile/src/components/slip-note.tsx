// Which slip a receipt is (specs/receipts.md, "A receipt is one slip"; the
// web client's SlipNote, word for word): the other copies of it, and
// receipts read exactly alike that are not — for those the person answers
// "Same receipt" or "Two purchases", once. The server decides which copy
// counts; this only says so.
import { Pressable, StyleSheet, Text, View } from "react-native";

import { errText, type ReceiptTwin } from "../lib/api";
import { C, mmddyy } from "../lib/theme";
import { useSlipAnswer } from "../lib/waiting";

/** Where the other receipt is (the web client's twinLabel). */
export function twinLabel(t: ReceiptTwin): string {
  const bits = [t.payee, t.date ? mmddyy(t.date) : null,
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
  const button = (label: string, other: string, same: boolean) => (
    <Pressable accessibilityRole="button" key={label}
               style={[s.btn, answer.isPending && { opacity: 0.5 }]}
               disabled={answer.isPending}
               onPress={() => answer.mutate({ id, other, same })}>
      <Text style={s.btnText}>{label}</Text>
    </Pressable>
  );
  return (
    <View style={{ gap: 4, marginTop: 4 }}>
      {twins.length > 0 && (
        <View style={s.row}>
          <Text style={[s.mut, { flex: 1 }]}>
            {counted
              ? `the same receipt as ${twinLabel(counted)} — its lines are `
                + "counted there, once"
              : `the same receipt as ${twinLabel(twins[0])} — its lines are `
                + "counted here, once"}
          </Text>
          {mayAnswer
            && button("Two purchases", (counted ?? twins[0]).receipt_id, false)}
        </View>)}
      {suspected.map((t) => (
        <View key={t.receipt_id} style={{ gap: 4 }}>
          <Text style={s.mut}>
            looks like the same receipt as {twinLabel(t)} —{" "}
            {suspectedNote(t, counts)}</Text>
          {mayAnswer && (
            <View style={s.row}>
              {button("Same receipt", t.receipt_id, true)}
              {button("Two purchases", t.receipt_id, false)}
            </View>)}
        </View>))}
      {answer.isError && (
        <Text style={{ color: C.bad, fontSize: 12 }}>
          {errText(answer.error)}</Text>)}
    </View>
  );
}

const s = StyleSheet.create({
  mut: { color: C.mut, fontSize: 12 },
  row: { flexDirection: "row", gap: 8, alignItems: "center",
         flexWrap: "wrap" },
  btn: { backgroundColor: C.btn, borderRadius: 8, paddingHorizontal: 10,
         paddingVertical: 5 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
