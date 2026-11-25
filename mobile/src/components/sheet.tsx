// The app's bottom sheet: a slide-up card over a dimmed backdrop that
// stays above the keyboard. With edge-to-edge on Android the window does
// not resize for the keyboard, so a sheet whose inputs sit at the
// bottom would have them typed under the keys — the avoiding view is what
// keeps the field being edited on screen. The body scrolls when the
// sheet is taller than the space left; the footer never scrolls away.
import { KeyboardAvoidingView, Modal, Pressable, ScrollView, StyleSheet,
         Text, View } from "react-native";

import { C } from "../lib/theme";
import { useSheetAnimation } from "../lib/a11y";

export default function Sheet({ visible, onClose, title, children, footer }: {
  visible: boolean; onClose: () => void; title?: string;
  children: React.ReactNode; footer?: React.ReactNode }) {
  const sheetAnim = useSheetAnimation();
  return (
    <Modal visible={visible} animationType={sheetAnim} transparent
           onRequestClose={onClose}>
      <KeyboardAvoidingView style={s.wrap} behavior="padding">
        {/* The sheet comes first in reading order, so a screen reader
            starts at its title; the tap-to-close backdrop follows it and
            is drawn beneath it. */}
        <View style={s.sheet}>
          {title ? <Text style={s.title}>{title}</Text> : null}
          <ScrollView keyboardShouldPersistTaps="handled"
                      nestedScrollEnabled
                      contentContainerStyle={{ gap: 6, paddingBottom: 4 }}>
            {children}
          </ScrollView>
          {footer}
        </View>
        <Pressable style={s.backdrop} onPress={onClose}
                   accessibilityRole="button" accessibilityLabel="Close" />
      </KeyboardAvoidingView>
    </Modal>
  );
}

/** the footer buttons draw 36pt tall; this reaches the 44pt target */
export const FOOTER_SLOP = { top: 4, bottom: 4 };

export const sheetStyles = StyleSheet.create({
  // the footer's row of buttons — Done / Clear / Close
  footer: { flexDirection: "row", gap: 8, justifyContent: "center",
            marginTop: 10 },
  btn: { backgroundColor: C.btn, borderRadius: 8,
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
           maxHeight: "88%", zIndex: 1 },
  backdrop: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0,
              zIndex: 0 },
  title: { color: C.text, fontSize: 16, fontWeight: "700",
           marginBottom: 6 },
});
