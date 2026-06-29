import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { SchemaForm } from "../components/SchemaForm";
import { theme } from "../theme";
import "../i18n"; // initialize the default i18n instance for useTranslation

const wrap = (schema: RJSFSchema, uiSchema?: UiSchema) =>
  render(
    <ThemeProvider theme={theme}>
      <SchemaForm schema={schema} uiSchema={uiSchema} />
    </ThemeProvider>,
  );

// Config form redo (#94): templates + PasswordWidget + inline errors.
describe("SchemaForm", () => {
  it("shows a required-field error inline on submit (no top summary)", () => {
    wrap({
      type: "object",
      required: ["name"],
      properties: { name: { type: "string", title: "Name" } },
    });
    // Submit with the required field empty.
    fireEvent.submit(screen.getByRole("button", { name: /submit/i }).closest("form")!);
    // The error renders inline near the field…
    expect(screen.getByText(/required/i)).toBeInTheDocument();
    // …and there is no top error-list summary heading (showErrorList=false).
    expect(screen.queryByText(/^Errors$/)).not.toBeInTheDocument();
  });

  it("renders a password field with a working show/hide toggle", () => {
    wrap(
      { type: "object", properties: { token: { type: "string", title: "Token" } } },
      { token: { "ui:widget": "password" } },
    );
    const input = screen.getByLabelText("Token") as HTMLInputElement;
    expect(input.type).toBe("password");
    // Toggle reveals the value (label flips show→hide).
    fireEvent.click(screen.getByLabelText(/show|显示/i));
    expect((screen.getByLabelText("Token") as HTMLInputElement).type).toBe("text");
  });

  it("keeps the path field mounted (degrades to text input outside Tauri)", () => {
    wrap(
      { type: "object", properties: { dir: { type: "string", title: "Folder" } } },
      { dir: { "ui:options": { taskpawPath: "directory" } } },
    );
    expect(screen.getByLabelText("Folder")).toBeInTheDocument();
    // No native dialog outside the Tauri shell → no Browse button.
    expect(screen.queryByLabelText(/browse/i)).not.toBeInTheDocument();
  });

  it("lays out multiple fields (two-column object template)", () => {
    wrap({
      type: "object",
      properties: {
        host: { type: "string", title: "Host" },
        port: { type: "integer", title: "Port" },
        enabled: { type: "boolean", title: "Enabled" },
      },
    });
    expect(screen.getByLabelText("Host")).toBeInTheDocument();
    expect(screen.getByLabelText("Port")).toBeInTheDocument();
    expect(screen.getByLabelText("Enabled")).toBeInTheDocument();
  });
});
