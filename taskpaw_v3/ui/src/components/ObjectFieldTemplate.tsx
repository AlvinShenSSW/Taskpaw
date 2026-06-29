import { Box, Typography } from "@mui/material";
import type { ObjectFieldTemplateProps } from "@rjsf/utils";

// Two-column form grid (#94, design preview `.form` / `.full`). Each property is a
// half-width cell; fields that need room span the full row:
//   - explicit `ui:options.full: true`
//   - nested objects/arrays (their own sub-grids)
//   - path fields (long absolute paths) and multiline/textarea
//   - booleans (a switch reads better on its own line)
// Single column on narrow widths (the add/edit dialog is ~480px).
function spansFull(name: string, props: ObjectFieldTemplateProps): boolean {
  const schemaProps = (props.schema.properties ?? {}) as Record<string, any>;
  const field = schemaProps[name] ?? {};
  const ui = (props.uiSchema?.[name] ?? {}) as Record<string, any>;
  const opts = ui["ui:options"] ?? {};
  if (opts.full === true) return true;
  if (field.type === "object" || field.type === "array") return true;
  if (field.type === "boolean") return true;
  const widget = ui["ui:widget"];
  if (widget === "TaskpawPath" || widget === "textarea" || widget === "password") return true;
  if (opts.taskpawPath) return true; // path hint even before the widget is wired
  return false;
}

export function ObjectFieldTemplate(props: ObjectFieldTemplateProps) {
  const { title, description, properties } = props;
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
        {properties.map((el) => (
          <Box
            key={el.name}
            sx={{ gridColumn: spansFull(el.name, props) ? "1 / -1" : "auto" }}
          >
            {el.content}
          </Box>
        ))}
      </Box>
    </Box>
  );
}
