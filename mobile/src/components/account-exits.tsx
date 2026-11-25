import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Pressable, StyleSheet, Text, TextInput,
         View } from "react-native";

import { Card, Note } from "./ui";
import { errText } from "../lib/api";
import { oneTimeCodeSentence } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { fieldA11y } from "./field";

// The two ways out of an account, shared by Security & household and by
// every screen that stands in front of the app (the second-factor lockout,
// an add-on's lockout). The server keeps both doors open behind those
// screens, so the screens have to offer them: a person who wants out must
// not be told to enrol a factor or pay first. One copy, so the typed
// confirmation and the wording cannot drift between the places that offer
// it. The web's components/AccountExits.tsx is the same pair.

// the web's error → sentence map, shared across the cards
export function explain(e: unknown): string {
  const s0 = String(e);
  if (s0.includes("totp_required"))
    return "Two-factor is on — enter your 6-digit code to confirm.";
  if (s0.includes("wrong password") || s0.includes("password_required"))
    return "That password didn't match.";
  const codeSentence = oneTimeCodeSentence(s0);
  if (codeSentence) return codeSentence;
  if (s0.includes("recovery_required"))
    return "This account signs in with a passkey — confirm with a "
      + "recovery code below.";
  if (s0.includes("too many requests"))
    return "Too many attempts — try again later.";
  if (s0.includes("already in use"))
    return "That email is already in use.";
  if (s0.includes("look like an email"))
    return "That doesn't look like an email.";
  if (s0.includes("at least 10"))
    return "New password must be at least 10 characters.";
  if (s0.includes("last_factor"))
    return "This is your only two-factor method — add another first.";
  // String(e) leads with "Error: " before the status, so stripping a
  // leading status alone still showed "Error: 409: {...}"
  return errText(e);
}

// ---- leave household (a member's own login; the web's card) ----
export function LeaveCard({ onDeleted }: { onDeleted: () => void }) {
  const { client } = useSession();
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => client!.accountLeave(),
    onSuccess: onDeleted,
    onError: (e) => setMsg(explain(e)),
  });
  return (
    <Card>
      <Text style={{ color: C.bad, fontSize: 16, fontWeight: "700" }}>
        Delete my login
      </Text>
      <Text style={s.mut}>
        Removes your login from this household and signs you out. The
        household's data and the owner's account are not affected — the
        owner can invite you again any time.
      </Text>
      <TextInput {...fieldA11y("Type “delete my login” to confirm", msg)} style={s.input}
                 placeholder="type “delete my login” to confirm"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={confirm} onChangeText={setConfirm} />
      {msg && (
        <Note bad style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Note>
      )}
      <Pressable accessibilityRole="button" style={[s.btn, { backgroundColor: C.danger,
                   alignSelf: "flex-start", marginTop: 6 },
                   (confirm !== "delete my login" || run.isPending)
                     && { opacity: 0.5 }]}
                 disabled={confirm !== "delete my login" || run.isPending}
                 onPress={() => run.mutate()}>
        <Text style={[s.btnText, { color: C.onDanger }]}>
          {run.isPending ? "deleting…" : "Delete my login"}
        </Text>
      </Pressable>
    </Card>
  );
}

// ---- delete account ----
// `exportHint` says where the copy is taken from on the screen showing the
// card — Security sends you to Settings, a lockout has no Settings behind it
export function DeleteCard({ onDeleted, exportHint }: {
  onDeleted: () => void; exportHint?: string;
}) {
  const { client } = useSession();
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => client!.accountDelete(),
    // the session is already dead server-side — drop the local one
    onSuccess: onDeleted,
    onError: (e) => setMsg(explain(e)),
  });
  // the typed phrase, then a native confirm, then the server's own fresh
  // "confirm it's you" — three doors, because there is no undo
  const ask = () => Alert.alert("Delete this account forever?",
    "Every account, transaction, budget and setting is erased and "
    + "everyone is signed out. This cannot be undone.",
    [{ text: "Delete everything", style: "destructive",
       onPress: () => run.mutate() },
     { text: "Cancel", style: "cancel" }]);
  return (
    <Card>
      <Text style={{ color: C.bad, fontSize: 16, fontWeight: "700" }}>
        Delete account
      </Text>
      <Text style={s.mut}>
        Erases this household permanently: every account, transaction,
        budget, and setting — and signs everyone out. There is no undo.{" "}
        {exportHint ?? "Export your data first (Settings → Your data) if "
                       + "you want a copy."}
      </Text>
      <TextInput {...fieldA11y("Type “delete everything” to confirm", msg)} style={s.input}
                 placeholder="type “delete everything” to confirm"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={confirm} onChangeText={setConfirm} />
      {msg && (
        <Note bad style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Note>
      )}
      <Pressable accessibilityRole="button" style={[s.btn, { backgroundColor: C.danger,
                   alignSelf: "flex-start", marginTop: 6 },
                   (confirm !== "delete everything" || run.isPending)
                     && { opacity: 0.5 }]}
                 disabled={confirm !== "delete everything" || run.isPending}
                 onPress={ask}>
        <Text style={[s.btnText, { color: C.onDanger }]}>
          {run.isPending ? "deleting…" : "Delete my account forever"}
        </Text>
      </Pressable>
    </Card>
  );
}

/** The way out on a screen that stands in front of the app: a quiet link
 *  that opens the card the caller's role is served. The owner erases the
 *  household (the leave door refuses an owner — it would orphan it);
 *  anyone else removes their own login (the erasure door is the owner's).
 *  A role not known yet offers neither, so no one is handed a door the
 *  server refuses on every press. Same labels and order as the web. */
export function AccountExit({ role, exportHint, onDone }: {
  role: string | null | undefined; exportHint?: string;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const owner = role === "owner";
  const other = !!role && role !== "owner";
  if (!owner && !other) return null;
  return (
    <View style={{ gap: 8 }}>
      <Pressable onPress={() => setOpen((o) => !o)} hitSlop={8}
                 accessibilityRole="button"
                 accessibilityState={{ expanded: open }}>
        <Text style={{ color: C.mut, fontSize: 13,
                       textDecorationLine: "underline" }}>
          {owner ? "Delete account" : "Leave household"}
        </Text>
      </Pressable>
      {open && (owner ? <DeleteCard onDeleted={onDone}
                                    exportHint={exportHint} />
                      : <LeaveCard onDeleted={onDone} />)}
    </View>
  );
}

const s = StyleSheet.create({
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
