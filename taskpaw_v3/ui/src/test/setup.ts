// Vitest setup (#45): jest-dom matchers + initialize i18next so components that
// call useTranslation render in tests. cleanup() runs after each test via the
// @testing-library/react auto-cleanup (vitest globals).
import "@testing-library/jest-dom/vitest";
import "../i18n";

// Fill jsdom's object URL gaps so deferred export cleanup survives restored globals.
URL.createObjectURL ??= () => "blob:jsdom";
URL.revokeObjectURL ??= () => {};
