// authenticated download → share sheet, with honest failure
import { useState } from "react";
import { Alert, Pressable, StyleSheet, Text } from "react-native";

import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { errText } from "../lib/api";

export function DownloadButton({ path, filename, label }: {
  path: string; filename: string; label: string;
}) {
  const { client } = useSession();
  const [busy, setBusy] = useState(false);
  return (
    <Pressable style={[s.btn, busy && { opacity: 0.5 }]}
               disabled={busy}
               onPress={async () => {
                 setBusy(true);
                 try { await client!.download(path, filename); }
                 catch (e) {
                   Alert.alert("Download failed", errText(e));
                 }
                 setBusy(false);
               }}>
      <Text style={s.btnText}>{busy ? "downloading…" : label}</Text>
    </Pressable>
  );
}

const s = StyleSheet.create({
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8,
         alignSelf: "flex-start", marginTop: 6 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
