const globals = require("globals");
const rules = {
  "no-undef": "error",
  "no-unused-vars": ["error", { args: "none" }],
  "no-redeclare": "error",
  complexity: ["error", 15],
  "max-depth": ["error", 4],
  "max-lines-per-function": ["error", 80],
};
const page = { ...globals.browser, Terminal: "readonly", FitAddon: "readonly", Format: "readonly" };
module.exports = [
  { files: ["web/page.js"], languageOptions: { ecmaVersion: 2022, sourceType: "script", globals: page }, rules },
  { files: ["web/format.js"], languageOptions: { ecmaVersion: 2022, sourceType: "script", globals: { ...globals.browser, module: "readonly" } }, rules },
];
