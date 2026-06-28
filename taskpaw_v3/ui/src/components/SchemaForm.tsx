import Form from "@rjsf/mui";
import validator from "@rjsf/validator-ajv8";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";

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
      uiSchema={uiSchema}
      validator={validator}
      formData={formData}
      onSubmit={(e) => onSubmit?.(e.formData)}
      liveValidate={false}
      showErrorList="top"
      focusOnFirstError
    />
  );
}
