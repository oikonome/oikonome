// The ledger assistant — ask about your money in plain words. The
// server does all the thinking; this screen is a question box and the
// running transcript of this visit (nothing is stored).
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text,
         TextInput, View } from "react-native";

import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { LogoSpinner } from "../components/logo-spinner";
import { errText } from "../lib/api";

interface Turn { id: number; q: string; a: string | null;
                 err?: string; tools?: string[] }

const EXAMPLES = [
  "What's my net worth?",
  "Where did my money go last month?",
  "What bills are coming up?",
  "Can I afford $2,000 this month?",
];

export default function Assistant() {
  const { client } = useSession();
  const [q, setQ] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const status = useQuery({
    queryKey: ["assistant-status"],
    queryFn: () => client!.assistantStatus(),
    enabled: !!client,
  });
  // turns key on an id, not the question text — asking the same
  // question twice must not write one answer into both bubbles
  const ask = useMutation({
    mutationFn: ({ question }: { id: number; question: string }) =>
      client!.assistantAsk(question),
    onSuccess: (d, v) =>
      setTurns((t) => t.map((x) =>
        x.id === v.id ? { ...x, a: d.answer, tools: d.tools_used } : x)),
    onError: (e, v) =>
      setTurns((t) => t.map((x) =>
        x.id === v.id ? { ...x, a: "", err: errText(e) } : x)),
  });
  const submit = () => {
    const question = q.trim();
    if (!question || ask.isPending) return;
    const id = Date.now();
    setTurns((t) => [...t, { id, q: question, a: null }]);
    setQ("");
    ask.mutate({ id, question });
  };

  if (status.data && !status.data.available) {
    return (
      <View style={s.wrap}>
        <Text style={s.center}>
          The assistant isn&apos;t enabled on this server — it needs an
          AI backend. Add one under Settings → AI (self-host installs
          can use the bundled Ollama).
        </Text>
      </View>
    );
  }
  return (
    <View style={s.wrap}>
      <ScrollView style={{ flex: 1 }}
                  contentContainerStyle={{ padding: 12, gap: 10 }}>
        {turns.length === 0 && (
          <Text style={s.center}>
            {status.data?.local === false && status.data.host
              ? `Ask about your money — answered from your own ledger; `
                + `your questions and the figures they return go to `
                + `${status.data.host}.`
              : "Ask about your money — everything is answered from your "
                + "own ledger, on this instance."}
          </Text>
        )}
        {/* the chips stay after the first question — that's exactly
            when someone wants the next one */}
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
          {EXAMPLES.map((ex) => (
            <Pressable key={ex} style={s.chip}
                       disabled={ask.isPending}
                       onPress={() => {
                         const id = Date.now();
                         setTurns((t) => [...t,
                           { id, q: ex, a: null }]);
                         ask.mutate({ id, question: ex });
                       }}>
              <Text style={{ color: C.mut, fontSize: 12 }}>{ex}</Text>
            </Pressable>
          ))}
        </View>
        {turns.map((t) => (
          <View key={t.id} style={{ gap: 6 }}>
            <View style={[s.bubble, s.me]}>
              <Text style={s.bubbleText}>{t.q}</Text>
            </View>
            <View style={[s.bubble, s.them]}>
              {t.a === null
                ? <LogoSpinner color={C.mut} />
                : t.err
                  ? <Text style={{ color: C.bad }}>{t.err}</Text>
                  : (
                    <>
                      {/* selectable = long-press copies the answer */}
                      <Text style={s.bubbleText} selectable>{t.a}</Text>
                      {(t.tools ?? []).length > 0 && (
                        <Text style={{ color: C.mut, fontSize: 10,
                                       marginTop: 4 }}>
                          from {t.tools!.join(" · ")}
                        </Text>
                      )}
                    </>
                  )}
            </View>
          </View>
        ))}
      </ScrollView>
      <View style={s.inputRow}>
        <TextInput style={s.input}
                   placeholder={turns.length
                     ? "ask a follow-up…" : "ask anything…"}
                   placeholderTextColor={C.mut} value={q}
                   onChangeText={setQ} onSubmitEditing={submit}
                   returnKeyType="send" />
        <Pressable style={[s.send, (!q.trim() || ask.isPending) && { opacity: 0.5 }]}
                   disabled={!q.trim() || ask.isPending} onPress={submit}>
          <Text style={s.sendText}>Ask</Text>
        </Pressable>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  bubble: { borderRadius: 12, padding: 10, maxWidth: "88%" },
  me: { backgroundColor: C.accent, alignSelf: "flex-end" },
  them: { backgroundColor: C.card, alignSelf: "flex-start",
          borderColor: C.border, borderWidth: 1 },
  bubbleText: { color: C.text, fontSize: 14, lineHeight: 20 },
  inputRow: { flexDirection: "row", gap: 8, padding: 10,
              borderTopColor: C.border, borderTopWidth: 1 },
  input: { flex: 1, backgroundColor: C.card, borderColor: C.border,
           borderWidth: 1, borderRadius: 10, color: C.text, fontSize: 15,
           paddingHorizontal: 12, paddingVertical: 9 },
  send: { backgroundColor: C.accent, borderRadius: 10,
          paddingHorizontal: 18, justifyContent: "center" },
  sendText: { color: C.text, fontSize: 15, fontWeight: "600" },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
});
