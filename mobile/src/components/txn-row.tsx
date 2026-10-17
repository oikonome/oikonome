// Shared ledger row: Today's recent pane and the Transactions page render
// the identical shape, mirroring the web's shared TxnTable.
// Sign convention: positive = money out; the display flips (inflows +green).
import { memo } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { Txn } from "../lib/api";
import { C, mmddyy, money } from "../lib/theme";
import { catLabel } from "../lib/pure";
import MerchantAvatar from "./merchant-avatar";

function TxnRow({ t, onPress, onLongPress, selected }:
    { t: Txn; onPress?: (t: Txn) => void;
      onLongPress?: (t: Txn) => void; selected?: boolean }) {
  const inflow = t.amount < 0;
  // transfers/card payments are not real spend — mute the amount so the
  // eye skips them, like the web's amount column
  const tcat = catLabel(t.category || "");
  const xfer = tcat.includes("TRANSFER")
    || (tcat.includes("LOAN PAYMENTS") && t.amount < 0);
  // paired payback (↩ reimb) and a still-awaiting flag are different
  // states — one is resolved, the other is money someone still owes
  // you, and the web NAMES both instead of leaving bare glyphs
  // the recurring mark is a link on the web, so it reads in the accent
  // colour there; here it carries the same colour so a bill's row stands
  // out from the muted marks around it
  const marks: { text: string; color?: string }[] = [
    { text: t.check_number ? `#${t.check_number}` : "" },
    { text: t.payment_processor ? `via ${t.payment_processor}` : "" },
    { text: t.payment_channel === "online" ? "online" : "" },
    { text: t.recurring_bill ? "⟳ recurring" : "", color: C.accent },
    { text: t.reimb ? "↩ reimb"
      : t.reimb_flag ? "⚑ awaiting reimbursement" : "" },
    { text: t.biz_flag ? "🏢 biz" : "" },
    { text: t.has_receipt ? "📎" : "" },
    // a split: the rollups count its parts, not the category shown — a
    // receipt's parts (the store matcher's reading of its items) say so
    { text: t.split?.length
      ? `✂ split ${t.split.length} ways${t.split_source && t.split_source !== "user" ? " by receipt" : ""}`
      : "" },
  ].filter((m) => m.text);
  return (
    <Pressable onPress={onPress ? () => onPress(t) : undefined}
               onLongPress={onLongPress ? () => onLongPress(t) : undefined}
               style={({ pressed }) => [s.row,
                 selected ? { backgroundColor: C.hover } : null,
                 pressed && onPress ? { backgroundColor: C.hover } : null]}>
      {selected !== undefined && (
        <Text style={{ color: selected ? C.accent : C.mut, fontSize: 16 }}>
          {selected ? "☑" : "☐"}
        </Text>
      )}
      <MerchantAvatar name={t.payee} logo={t.merchant_logo} />
      <View style={s.left}>
        <Text style={s.payee} numberOfLines={1}>
          {t.payee}
          {t.item_summary ? <Text style={s.mut}> · {t.item_summary}</Text> : null}
        </Text>
        {/* the row IS a business's money (its account belongs to the
            entity) — named, in the entity colour, as on the web; distinct
            from the 🏢 biz mark, which is a household charge tagged as a
            business expense */}
        {t.entity ? (
          <View style={s.entWrap}>
            <View style={s.ent}><Text style={s.entText}>{t.entity}</Text></View>
          </View>
        ) : null}
        <Text style={s.mut} numberOfLines={1}>
          {mmddyy(t.date)} · {catLabel(t.category)}
          {t.override_is_fee && t.override_bill
            ? ` · fee of ${t.override_bill}` : ""}
          {t.pending ? " · pending" : ""}
          {marks.map((m, i) => (
            <Text key={i}>
              {i ? " · " : "  "}
              <Text style={m.color ? { color: m.color, fontWeight: "600" } : null}>
                {m.text}
              </Text>
            </Text>
          ))}
          {t.note ? <Text style={{ fontStyle: "italic" }}> ✎ {t.note}</Text>
                  : null}
        </Text>
      </View>
      <Text style={[s.amount, xfer ? { color: C.mut }
                              : inflow ? { color: C.good } : null]}>
        {inflow ? `+${money(-t.amount)}` : money(t.amount)}
      </Text>
    </Pressable>
  );
}

// rows re-render only when their own transaction changes — a list of a
// month's ledger must not repaint wholesale on every parent render
export default memo(TxnRow);

const s = StyleSheet.create({
  row: { flexDirection: "row", alignItems: "center", gap: 10,
         paddingVertical: 7, paddingHorizontal: 4, borderRadius: 8,
         marginHorizontal: -4 },
  left: { flex: 1, gap: 1 },
  payee: { color: C.text, fontSize: 14, fontWeight: "500" },
  mut: { color: C.mut, fontSize: 12, fontWeight: "400" },
  entWrap: { flexDirection: "row", marginTop: 1 },
  ent: { backgroundColor: "rgba(160,120,220,.18)", borderRadius: 999,
         paddingHorizontal: 7, paddingVertical: 1 },
  entText: { color: C.entity, fontSize: 11, fontWeight: "600" },
  amount: { color: C.text, fontSize: 14, fontVariant: ["tabular-nums"] },
});
