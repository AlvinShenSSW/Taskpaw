import { Box, Checkbox, FormControlLabel, FormHelperText, TextField, Typography } from "@mui/material";
import type { ObjectFieldTemplateProps, RJSFSchema, UiSchema } from "@rjsf/utils";
import { createContext, useContext, type ReactNode } from "react";

// Two-column form grid (#94, design preview `.form` / `.full`). Each property is a
// half-width cell; fields that need room span the full row:
//   - explicit `ui:options.full: true`
//   - nested objects/arrays (their own sub-grids)
//   - path fields (long absolute paths) and multiline/textarea
//   - booleans (a switch reads better on its own line)
// Single column on narrow widths (the add/edit dialog is ~480px).
function spansFull(name: string, props: ObjectFieldTemplateProps): boolean {
  const schemaProps = (props.schema.properties ?? {}) as Record<string, RJSFSchema>;
  const field = schemaProps[name] ?? {};
  const ui = (props.uiSchema?.[name] ?? {}) as UiSchema<unknown>;
  const opts = ui["ui:options"] ?? {};
  if (opts.full === true) return true;
  if (field.type === "object" || field.type === "array") return true;
  if (field.type === "boolean") return true;
  const widget = ui["ui:widget"];
  if (widget === "TaskpawPath" || widget === "textarea" || widget === "password") return true;
  if (opts.taskpawPath) return true; // path hint even before the widget is wired
  return false;
}

// Fields locked to fixed values (#208, the Jasna「8K VR」profile): each shows a
// greyed display control with the fixed value, while the real rjsf field stays
// mounted but hidden so rjsf keeps and submits the operator's own value. `note`
// renders as a full row after the property named by `after`.
export type LockedFields = {
  after: string;
  note: ReactNode;
  fields: Record<string, { value: string | number | boolean }>;
};

// Travels by context, never through a Form prop: any rjsf Form prop change
// (uiSchema, formContext) rebuilds the form state and drops unsaved edits (#204).
const LockedFieldsContext = createContext<LockedFields | undefined>(undefined);

export function LockedFieldsProvider({ value, children }: { value?: LockedFields; children: ReactNode }) {
  return <LockedFieldsContext.Provider value={value}>{children}</LockedFieldsContext.Provider>;
}

// Decorative only: MUI-disabled, out of the tab order, its own id (never rjsf's
// `root_*`), label and help from the localized schema.
function LockedDisplay({ name, value, field }: { name: string; value: string | number | boolean; field: RJSFSchema }) {
  const id = `locked_${name}`;
  const label = field.title ?? name;
  if (typeof value === "boolean") {
    return (
      <Box>
        <FormControlLabel
          disabled
          label={label}
          control={<Checkbox id={id} checked={value} disabled tabIndex={-1} />}
        />
        {field.description && <FormHelperText disabled>{field.description}</FormHelperText>}
      </Box>
    );
  }
  return (
    <TextField
      id={id}
      label={label}
      helperText={field.description}
      value={String(value)}
      disabled
      fullWidth
      slotProps={{ htmlInput: { tabIndex: -1 } }}
    />
  );
}

export function ObjectFieldTemplate(props: ObjectFieldTemplateProps) {
  const { title, description, properties } = props;
  const locked = useContext(LockedFieldsContext);
  // Hidden fields (e.g. the base caps `max_events_per_minute`/`max_line_bytes`,
  // marked `ui:widget: hidden`) render bare so their inputs still submit without
  // leaving empty grid cells that gap/misalign the visible fields (Codex).
  const hidden = properties.filter((el) => el.hidden);
  const visible = properties.filter((el) => !el.hidden);
  const schemaProps = (props.schema.properties ?? {}) as Record<string, RJSFSchema>;
  // A locked field whose live value is invalid stays visible: a hidden invalid
  // input would make the browser block submit silently (not focusable). An
  // undefined (cleared) value counts as valid — the submit then omits the key.
  const showsDisplay = (name: string): boolean => {
    if (!locked || !Object.hasOwn(locked.fields, name)) return false;
    if (props.errorSchema?.[name]?.__errors?.length) return false;
    const v = (props.formData as Record<string, unknown> | undefined)?.[name];
    return v === undefined || props.registry.schemaUtils.getValidator()
      .isValid(schemaProps[name] ?? {}, v, props.registry.rootSchema);
  };
  return (
    <Box>
      {title && (
        <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
          {title}
        </Typography>
      )}
      {description && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {description}
        </Typography>
      )}
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
          gap: 2,
          alignItems: "start",
        }}
      >
        {visible.flatMap((el) => {
          const display = showsDisplay(el.name);
          const cell = (
            <Box
              key={el.name}
              sx={{ gridColumn: spansFull(el.name, props) ? "1 / -1" : "auto" }}
            >
              {display && (
                <LockedDisplay name={el.name} value={locked!.fields[el.name].value}
                  field={schemaProps[el.name] ?? {}} />
              )}
              {/* Always this same Box, so the rjsf input keeps its DOM node across lock/unlock. */}
              <Box sx={display ? { display: "none" } : undefined}>{el.content}</Box>
            </Box>
          );
          return locked?.after === el.name
            ? [cell, <Box key={`${el.name}__locked_note`} sx={{ gridColumn: "1 / -1" }}>{locked.note}</Box>]
            : [cell];
        })}
      </Box>
      {hidden.map((el) => (
        <span key={el.name}>{el.content}</span>
      ))}
    </Box>
  );
}
