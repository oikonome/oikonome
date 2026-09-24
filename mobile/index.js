// The app's entry: expo-router's, plus the Android widget's headless
// task. Registered here, before any screen, because the OS can wake the
// task with no screen mounted (the half-hourly update, a widget being
// added). Android only — on iOS the widget is a separate Swift
// extension (targets/glance) and the library's native module is absent.
import "expo-router/entry";
import { Platform } from "react-native";

if (Platform.OS === "android") {
  const { registerWidgetTaskHandler } = require("react-native-android-widget");
  const { widgetTaskHandler } = require("./src/widgets/android");
  registerWidgetTaskHandler(widgetTaskHandler);
}
