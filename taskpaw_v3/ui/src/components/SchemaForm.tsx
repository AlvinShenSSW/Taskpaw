import Form, { Templates } from "@rjsf/mui";
import validator from "@rjsf/validator-ajv8";
import type {
  RegistryWidgetsType,
  RJSFSchema,
  TemplatesType,
  UiSchema,
  SubmitButtonProps,
} from "@rjsf/utils";
import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { PathWidget } from "./PathWidget";
import { PasswordWidget } from "./PasswordWidget";
import { LockedFieldsProvider, ObjectFieldTemplate, type LockedFields } from "./ObjectFieldTemplate";
import { localizeSchema } from "../schemaI18n";

const widgets: RegistryWidgetsType = {
  TaskpawPath: PathWidget,
  // Override the default `password` widget with the show/hide one (#94); also
  // catches json-schema `format: "password"` fields.
  password: PasswordWidget,
};

const ReminderContext = createContext<ReactNode>(null);
// The MUI theme supplies the complete button set (its export type is partial).
const buttonTemplates = Templates.ButtonTemplates!;
const DefaultSubmitButton = buttonTemplates.SubmitButton;

// Keep both the template and Form props stable: a reminder in formContext (or
// a per-render template) makes rjsf restore props.formData over unsaved edits.
function SubmitButton(props: SubmitButtonProps) {
  const reminder = useContext(ReminderContext);
  return <>{reminder}<DefaultSubmitButton {...props} /></>;
}

const templates: Partial<TemplatesType> = {
  // Two-column field grid with full-span support (design preview `.form`).
  ObjectFieldTemplate,
  ButtonTemplates: { ...buttonTemplates, SubmitButton },
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
  onChange,
  reminder,
  locked,
  typeId,
}: {
  schema: RJSFSchema;
  uiSchema?: UiSchema;
  formData?: unknown;
  onSubmit?: (data: unknown) => void;
  onChange?: (data: unknown) => void;
  reminder?: ReactNode;
  // Fields shown greyed with fixed values while rjsf keeps the stored ones (#208);
  // provided by context like the reminder, so the Form props never change.
  locked?: LockedFields;
  // The plugin type_id, so field labels/help can be localized (#121).
  typeId?: string;
}) {
  const { i18n } = useTranslation();
  // Overlay zh field labels/help onto the backend English schema when the UI is in
  // Chinese; untranslated fields keep their English (#121). Recompute on lang switch.
  const localizedSchema = useMemo(
    () => localizeSchema(schema, typeId, i18n.language),
    [schema, typeId, i18n.language],
  );
  return (
    <ReminderContext.Provider value={reminder}>
      <LockedFieldsProvider value={locked}>
        <Form
          schema={localizedSchema}
          uiSchema={withPathWidgets(uiSchema)}
          widgets={widgets}
          templates={templates}
          validator={validator}
          formData={formData}
          onSubmit={(e) => onSubmit?.(e.formData)}
          onChange={(e) => onChange?.(e.formData)}
          liveValidate={false}
          showErrorList={false}
          focusOnFirstError
        />
      </LockedFieldsProvider>
    </ReminderContext.Provider>
  );
}
