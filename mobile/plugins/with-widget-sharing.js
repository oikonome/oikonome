// Expo config plugin: the app group the iOS home-screen widget shares
// with the app. `group.<bundle id>` — derived at prebuild time so a
// self-hoster's own bundle id and the store build both get a matching
// pair without editing app.json (whose ios.entitlements cannot reference
// the id). The widget target (targets/glance/expo-target.config.js)
// derives the same name; src/lib/glance.ts writes the widget's
// credential into the Keychain under it, and the extension reads it.
// Android needs nothing here: the widget task runs in the app's process.
const { withEntitlementsPlist } = require("expo/config-plugins");

const GROUPS_KEY = "com.apple.security.application-groups";

module.exports = function withWidgetSharing(config) {
  return withEntitlementsPlist(config, (c) => {
    const id = c.ios?.bundleIdentifier;
    if (!id) return c;
    const group = `group.${id}`;
    const have = Array.isArray(c.modResults[GROUPS_KEY])
      ? c.modResults[GROUPS_KEY] : [];
    if (!have.includes(group)) c.modResults[GROUPS_KEY] = [...have, group];
    return c;
  });
};
