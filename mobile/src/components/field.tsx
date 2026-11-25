// A field's spoken name and error. A placeholder is spoken only while the
// field is empty, so once someone types "1250" a field named only by its
// placeholder reads as "1250, text field" with no name. Every TextInput
// takes its name from the words on screen that label it — the Text above
// it, the row it sits in, its column — and the error shown for it is
// spoken with it, the phone's counterpart of the web's <label htmlFor>
// and aria-describedby. See specs/accessibility.md.
//
// It is a set of props, not a wrapper component: every screen lays its
// label and field out in its own way, and a wrapper would change that.
import type { TextInputProps } from "react-native";

type FieldA11y = Pick<TextInputProps, "accessibilityLabel" | "accessibilityHint">;

/** Spread on a TextInput: `<TextInput {...fieldA11y("Amount", err)} />`. */
export function fieldA11y(label: string, error?: string | null): FieldA11y {
  return { accessibilityLabel: label, accessibilityHint: error || undefined };
}
