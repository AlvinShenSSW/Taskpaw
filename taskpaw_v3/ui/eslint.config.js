// ESLint flat config for the V3 UI (#141) — React 19 + TypeScript + Vite.
// A pragmatic first gate: JS + typescript-eslint recommended (no type-checked
// rules, so it stays fast and needs no project graph) + React Hooks rules +
// the Vite react-refresh guard. `npm run lint` runs it; CI runs `npm run lint`.
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "node_modules"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      // Dynamic rjsf / JSON-Schema glue (uiSchema, schema.properties, MUI style
      // overrides) is genuinely open-shaped; `any` there is pragmatic. Surface it
      // as a warning to discourage new uses without blocking this first gate.
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  // Test files + setup run under jsdom/vitest — allow those globals.
  {
    files: ["src/test/**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.node } },
  },
  // Node context for build/config files.
  {
    files: ["*.{js,ts}", "vite.config.ts"],
    languageOptions: { globals: { ...globals.node } },
  },
);
