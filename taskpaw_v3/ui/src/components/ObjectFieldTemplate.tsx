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
  // Hidden fields (e.g. the base caps `max_events_per_minute`/`max_line_bytes`,
  // marked `ui:widget: hidden`) render bare so their inputs still submit without
  // leaving empty grid cells that gap/misalign the visible fields (Codex).
  const hidden = properties.filter((el) => el.hidden);
  const visible = properties.filter((el) => !el.hidden);
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
        {visible.map((el) => (
          <Box
            key={el.name}
            sx={{ gridColumn: spansFull(el.name, props) ? "1 / -1" : "auto" }}
          >
            {el.content}
          </Box>
        ))}
      </Box>
      {hidden.map((el) => (
        <span key={el.name}>{el.content}</span>
      ))}
    </Box>
  );
}
