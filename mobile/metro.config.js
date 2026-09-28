// Metro with inline requires: a module is evaluated the first time a
// screen reaches into it, not at bundle load. The root layout imports the
// API client, the elevation modal, passkeys and the session machinery
// wholesale; with lazy evaluation the first frame waits only for what it
// draws.
const { getDefaultConfig } = require("expo/metro-config");

const config = getDefaultConfig(__dirname);
config.transformer = {
  ...config.transformer,
  getTransformOptions: async () => ({
    transform: { experimentalImportSupport: true, inlineRequires: true },
  }),
};

module.exports = config;
