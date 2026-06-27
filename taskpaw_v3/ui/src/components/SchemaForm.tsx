import Form from "@rjsf/mui";
import validator from "@rjsf/validator-ajv8";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";

// Schema-driven monitor config form (design §4.3). The plugin's json_schema
// (from the backend) drives the fields; secret fields render as password and
// show "***" for stored values (the backend masks them).
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
      showErrorList={false}
    />
  );
}
