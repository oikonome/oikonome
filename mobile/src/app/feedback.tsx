// Feedback & bug reports — a message (and optionally a screenshot)
// straight to the operator, numbered like the web page's flow. One form,
// two kinds: feedback, or a bug report shaped by three prompts, so a
// person who hit a defect recognises this as the place to say so.
import { useMutation, useQuery } from "@tanstack/react-query";
import * as ImagePicker from "expo-image-picker";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput,
         View } from "react-native";

import { Card, H } from "../components/ui";
import { PickedFile } from "../lib/api";
import { composeBugReport } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";

type Kind = "feedback" | "bug";

export default function Feedback() {
  const { client } = useSession();
  const testing = useQuery({ queryKey: ["testing"],
    queryFn: () => client!.testing(), enabled: !!client });
  const [kind, setKind] = useState<Kind>("feedback");
  const [message, setMessage] = useState("");
  const [happened, setHappened] = useState("");
  const [expected, setExpected] = useState("");
  const [where, setWhere] = useState("");
  const [shot, setShot] = useState<PickedFile | null>(null);
  const text = kind === "bug"
    ? composeBugReport(happened, expected, where) : message.trim();
  const send = useMutation({
    mutationFn: () => client!.testingFeedback(text, shot, kind),
    onSuccess: () => { setMessage(""); setHappened(""); setExpected("");
                       setWhere(""); setShot(null); },
  });
  const pickShot = async () => {
    const res = await ImagePicker.launchImageLibraryAsync(
      { mediaTypes: ["images"], quality: 0.8 });
    if (!res.canceled && res.assets[0])
      setShot({ uri: res.assets[0].uri,
                name: res.assets[0].fileName ?? "screenshot.jpg",
                mime: res.assets[0].mimeType ?? "image/jpeg" });
  };
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}>
      <Card>
        <H>Feedback & bug reports</H>
        <View style={s.seg} accessibilityRole="radiogroup">
          {(["feedback", "bug"] as Kind[]).map((k) => (
            <Pressable key={k} accessibilityRole="radio"
                       accessibilityState={{ checked: kind === k }}
                       style={[s.segBtn, kind === k && s.segOn]}
                       onPress={() => setKind(k)}>
              <Text style={[s.segText, kind === k && { color: C.text }]}>
                {k === "bug" ? "Report a bug" : "Feedback"}
              </Text>
            </Pressable>
          ))}
        </View>
        <Text style={s.mut}>
          {kind === "bug"
            ? "Something broke? Say what happened, what you expected, and "
              + "where — a screenshot helps a lot. "
            : "Something confusing, missing, or worth changing? Tell us. "}
          Goes straight to the instance operator. The report carries the
          app version, a system-health diagnostic bundle and recent log
          lines — so what broke is diagnosable.
          {testing.data?.feedback_to
            ? ` Delivered to ${testing.data.feedback_to}.` : ""}
        </Text>
        {testing.data && testing.data.enabled === false && (
          <Text style={{ color: C.warn, fontSize: 12, marginTop: 4 }}>
            Feedback is turned off on this instance.
          </Text>
        )}
        {kind === "bug" ? (
          <>
            <TextInput style={s.input} multiline
                       placeholder="What happened?"
                       accessibilityLabel="What happened"
                       placeholderTextColor={C.mut}
                       value={happened} onChangeText={setHappened} />
            <TextInput style={[s.input, s.inputShort]} multiline
                       placeholder="What did you expect instead?"
                       accessibilityLabel="What you expected"
                       placeholderTextColor={C.mut}
                       value={expected} onChangeText={setExpected} />
            <TextInput style={[s.input, s.inputLine]}
                       placeholder="Which screen were you on?"
                       accessibilityLabel="Where it happened"
                       placeholderTextColor={C.mut}
                       value={where} onChangeText={setWhere} />
          </>
        ) : (
          <TextInput style={s.input} multiline
                     placeholder="what you'd change, what was confusing, what you'd want…"
                     accessibilityLabel="Your feedback"
                     placeholderTextColor={C.mut}
                     value={message} onChangeText={setMessage} />
        )}
        <View style={{ flexDirection: "row", gap: 8, marginTop: 8,
                       alignItems: "center" }}>
          <Pressable style={[s.btn, s.btnQuiet]} onPress={pickShot}>
            <Text style={[s.btnText, { color: C.mut }]}>
              {shot ? "replace screenshot" : "attach screenshot"}
            </Text>
          </Pressable>
          {shot ? (
            <Text style={s.mut} onPress={() => setShot(null)}>
              {shot.name} ✕
            </Text>
          ) : null}
        </View>
        {send.isError ? (
          <Text style={{ color: C.bad, fontSize: 12, marginTop: 6 }}>
            {String(send.error)}
          </Text>
        ) : null}
        {send.isSuccess ? (
          <Text style={{ color: C.good, fontSize: 12, marginTop: 6 }}>
            {send.data.delivery === "zip"
              ? `Packaged as feedback #${send.data.number || "?"} — this `
                + "instance has no email set up, so the report was built "
                + "as a .zip and offered to your share sheet. Attach it "
                + "to an email by hand."
              : `Sent — ${kind === "bug" ? "bug report" : "feedback"} `
                + `#${send.data.number}. Thank you.`}
          </Text>
        ) : null}
        <Pressable style={[s.btn, { alignSelf: "flex-start",
                     marginTop: 10 },
                     (!text.trim() || send.isPending)
                       && { opacity: 0.5 }]}
                   disabled={!text.trim() || send.isPending}
                   onPress={() => send.mutate()}>
          <Text style={s.btnText}>
            {send.isPending ? "sending…" : "Send"}
          </Text>
        </Pressable>
      </Card>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, minHeight: 120,
           textAlignVertical: "top", marginTop: 8 },
  inputShort: { minHeight: 72 },
  inputLine: { minHeight: 40 },
  seg: { flexDirection: "row", gap: 6, marginTop: 6, marginBottom: 4 },
  segBtn: { backgroundColor: C.hover, borderRadius: 8,
            paddingHorizontal: 12, paddingVertical: 6 },
  segOn: { backgroundColor: C.accent },
  segText: { color: C.mut, fontSize: 13, fontWeight: "600" },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
