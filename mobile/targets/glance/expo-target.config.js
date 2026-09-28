// The iOS home-screen widget: a WidgetKit extension generated into the
// Xcode project by @bacons/apple-targets at `npx expo prebuild`. The
// Swift lives beside this file (index.swift); nothing under ios/ is
// source. The app group is the one plugins/with-widget-sharing.js grants
// the app, derived from the bundle id so a self-hoster's own id and the
// store build both work without editing anything here.
/** @type {import('@bacons/apple-targets/app.plugin').ConfigFunction} */
module.exports = (config) => ({
  type: "widget",
  name: "glance",
  displayName: "Oikonome",
  deploymentTarget: "17.0",
  // `<app bundle id>.glance` — index.swift strips this suffix to find the
  // app group, so the name is fixed here rather than left to the default
  bundleIdentifier: ".glance",
  frameworks: ["SwiftUI", "WidgetKit"],
  colors: {
    // referenced by the generated Info.plist; the views set their own
    $widgetBackground: { light: "#ffffff", dark: "#1a212b" },
    $accent: "#4f9bd6",
  },
  entitlements: {
    "com.apple.security.application-groups": [
      `group.${config.ios.bundleIdentifier}`,
    ],
  },
});
