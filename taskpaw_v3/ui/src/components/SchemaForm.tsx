import Form from "@rjsf/mui";
import validator from "@rjsf/validator-ajv8";
import type { RegistryWidgetsType, RJSFSchema, UiSchema } from "@rjsf/utils";
import { PathWidget } from "./PathWidget";

const widgets: RegistryWidgetsType = { TaskpawPath: PathWidget };

// Fields the backend marks with `ui:options.taskpawPath` (lada_cli_path, the
// folders, comfyui_log_path, …) get the native file/directory picker widget (#71)
// — set ui:widget without touching the backend ui_schema (which only carries the
// path KIND in ui:options). Shallow per-field: that's the shape the catalog emits.
function withPathWidgets(ui?: UiSchema): UiSchema {
  if (!ui) return {};
  const out: UiSchema = { ...ui };
  for (const [key, entry] of Object.entries(ui)) {
    const opts = (entry as { "ui:options"?: { taskpawPath?: unknown } })?.["ui:options"];
    if (opts && opts.taskpawPath) {
      out[key] = { ...(entry as object), "ui:widget": "TaskpawPath" };
    }
  }
  return out;
}

// Schema-driven monitor config form (design §4.3). The plugin's json_schema
// (from the backend) drives the fields; field descriptions render as help text.
//
// Validation errors are SHOWN on submit (a summary at the top + focusing the
// first bad field) so a failed submit — e.g. a missing required field — is never
// a silent no-op (#70). (No liveValidate: don't flag a pristine, untouched form.)
//
// NOTE: the ajv8 validator compiles schemas with `new Function` (eval), so the
// Tauri webview CSP must allow 'unsafe-eval' in script-src (tauri.conf.json) —
// otherwise validateFormData throws a CSP error on submit and nothing happens.
export function SchemaForm({
  schema,
  uiSchema,
  formData,
  onSubmit,
}: {
  schema: RJSFSchema;
  uiSchema?: UiSchema;
  formData?: unknown;
  onSubmit?: (data: unknown) => void;
}) {
  return (
    <Form
      schema={schema}
      uiSchema={withPathWidgets(uiSchema)}
      widgets={widgets}
      validator={validator}
      formData={formData}
      onSubmit={(e) => onSubmit?.(e.formData)}
      liveValidate={false}
      showErrorList="top"
      focusOnFirstError
    />
  );
}
