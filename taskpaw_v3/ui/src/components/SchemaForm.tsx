import Form from "@rjsf/mui";
import validator from "@rjsf/validator-ajv8";
import type {
  RegistryWidgetsType,
  RJSFSchema,
  TemplatesType,
  UiSchema,
} from "@rjsf/utils";
import { PathWidget } from "./PathWidget";
import { PasswordWidget } from "./PasswordWidget";
import { ObjectFieldTemplate } from "./ObjectFieldTemplate";

const widgets: RegistryWidgetsType = {
  TaskpawPath: PathWidget,
  // Override the default `password` widget with the show/hide one (#94); also
  // catches json-schema `format: "password"` fields.
  password: PasswordWidget,
};

const templates: Partial<TemplatesType> = {
  // Two-column field grid with full-span support (design preview `.form`).
  ObjectFieldTemplate,
};

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

// Schema-driven monitor config form (design §4.3 + §6 redo). The plugin's
// json_schema (from the backend) drives the fields; the custom ObjectFieldTemplate
// lays them out in a two-column grid (label above, helper below, required `*`,
// focus glow from the theme), and PasswordWidget gives secret fields a show/hide.
//
// Validation errors render INLINE next to each field (showErrorList=false drops
// the redundant top summary), and a failed submit focuses the first bad field so
// it's never a silent no-op (#70/#94). (No liveValidate: don't flag a pristine,
// untouched form.)
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
      templates={templates}
      validator={validator}
      formData={formData}
      onSubmit={(e) => onSubmit?.(e.formData)}
      liveValidate={false}
      showErrorList={false}
      focusOnFirstError
    />
  );
}
