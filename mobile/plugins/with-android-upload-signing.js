// Expo config plugin: sign release builds with the upload keystore.
//
// mobile/android/ is a prebuild output, and Expo's template signs
// `release` with the DEBUG keystore, and every prebuild regenerates that
// block — an APK signed with the debug key will not install over a release
// signed with the upload key (INSTALL_FAILED_UPDATE_INCOMPATIBLE). So the
// signing config lives here, where prebuild re-applies it, and reads the key material
// from ~/.gradle/gradle.properties (OIKONOME_UPLOAD_STORE_FILE, _KEY_ALIAS,
// _STORE_PASSWORD, optional _KEY_PASSWORD) — never from the repo. When
// those properties are absent (CI, a fresh clone) the template's debug
// signing stays, so a build never fails for lack of a key it should not
// have anyway.
const { withAppBuildGradle } = require("expo/config-plugins");

const RELEASE_SIGNING = `
        release {
            // upload keystore from ~/.gradle/gradle.properties; absent →
            // stays unsigned-for-release (debug key), see
            // plugins/with-android-upload-signing.js
            if (project.hasProperty('OIKONOME_UPLOAD_STORE_FILE')) {
                storeFile file(OIKONOME_UPLOAD_STORE_FILE)
                storePassword OIKONOME_UPLOAD_STORE_PASSWORD
                keyAlias OIKONOME_UPLOAD_KEY_ALIAS
                keyPassword project.hasProperty('OIKONOME_UPLOAD_KEY_PASSWORD')
                    ? OIKONOME_UPLOAD_KEY_PASSWORD : OIKONOME_UPLOAD_STORE_PASSWORD
            }
        }`;

module.exports = function withAndroidUploadSigning(config) {
  return withAppBuildGradle(config, (c) => {
    let g = c.modResults.contents;
    if (g.includes("OIKONOME_UPLOAD_STORE_FILE")) return c;   // already applied
    // add the release signing config next to the template's debug one
    g = g.replace(
      /signingConfigs \{\n(\s+)debug \{[\s\S]*?\n\1\}\n/,
      (m) => m + RELEASE_SIGNING + "\n");
    // and make the release build type use it when the key is present
    g = g.replace(
      // the template has written both `signingConfig signingConfigs.debug`
      // and, since Expo 57, `signingConfig = signingConfigs.debug`; a miss
      // here is silent and ships a debug-signed bundle the store refuses
      /(release \{\n\s+\/\/ Caution![^\n]*\n\s+\/\/ see[^\n]*\n)\s+signingConfig (?:= )?signingConfigs\.debug/,
      "$1            signingConfig = project.hasProperty('OIKONOME_UPLOAD_STORE_FILE')"
      + " ? signingConfigs.release : signingConfigs.debug");
    // a template this regex does not recognise must stop the build here,
    // not at the store's door
    if (!/signingConfig = project\.hasProperty\('OIKONOME_UPLOAD_STORE_FILE'\)/.test(g)) {
      throw new Error("with-android-upload-signing: the release build type's signingConfig line was not found in app/build.gradle; the plugin's pattern needs updating for this template");
    }
    c.modResults.contents = g;
    return c;
  });
};
