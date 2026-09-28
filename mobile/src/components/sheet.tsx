// The app's bottom sheet: a slide-up card over a dimmed backdrop that
// stays above the keyboard. With edge-to-edge on Android the window does
// not resize for the keyboard, so a sheet whose inputs sit at the
// bottom would have them typed under the keys — the avoiding view is what
// keeps the field being edited on screen. The body scrolls when the
// sheet is taller than the space left; the footer never scrolls away.
import { KeyboardAvoidingView, Modal, Pressable, ScrollView, StyleSheet,
         Text, View } from "react-native";

import { C } from "../lib/theme";

export default function Sheet({ visible, onClose, title, children, footer }: {
  visible: boolean; onClose: () => void; title?: string;
  children: React.ReactNode; footer?: React.ReactNode }) {
  return (
    <Modal visible={visible} animationType="slide" transparent
           onRequestClose={onClose}>
      <KeyboardAvoidingView style={s.wrap} behavior="padding">
        <Pressable style={{ flex: 1 }} onPress={onClose}
                   accessibilityLabel="close" />
        <View style={s.sheet}>
          {title ? <Text style={s.title}>{title}</Text> : null}
          <ScrollView keyboardShouldPersistTaps="handled"
                      nestedScrollEnabled
                      contentContainerStyle={{ gap: 6, paddingBottom: 4 }}>
            {children}
          </ScrollView>
          {footer}
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

export const sheetStyles = StyleSheet.create({
  // the footer's row of buttons — Done / Clear / Close
  footer: { flexDirection: "row", gap: 8, justifyContent: "center",
            marginTop: 10 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 20, paddingVertical: 9 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontWeight: "600" },
  btnQuietText: { color: C.mut, fontWeight: "600" },
});

const s = StyleSheet.create({
  wrap: { flex: 1, justifyContent: "flex-end",
          backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, paddingBottom: 20,
           maxHeight: "88%" },
  title: { color: C.text, fontSize: 16, fontWeight: "700",
           marginBottom: 6 },
});
