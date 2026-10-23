import { InputAdornment, TextField, Tooltip } from "@mui/material";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import IconButton from "@mui/material/IconButton";
import type { WidgetProps } from "@rjsf/utils";
import { inTauri, pickPath } from "./nativePicker";

// rjsf widget for path fields (#71): a text input + a Browse button that opens
// the NATIVE Tauri file/directory picker and writes the chosen absolute path back
// into the form. The field's `ui:options.taskpawPath` ("file" | "directory")
// chooses the dialog mode. Outside the Tauri shell (browser/dev) the Browse
// button is hidden and it degrades to a plain text input — a web <input
// type=file> can't expose a real filesystem path anyway.
export function PathWidget(props: WidgetProps) {
  const { id, value, onChange, label, options, disabled, readonly, required, autofocus } = props;
  const directory = (options?.taskpawPath as string) === "directory";

  const browse = async () => {
    // Dialog unavailable/cancelled (null) — leave the typed value untouched.
    const picked = await pickPath(directory, `Select ${directory ? "folder" : "file"}${label ? ` — ${label}` : ""}`);
    if (picked !== null) onChange(picked);
  };

  const editable = !disabled && !readonly;
  return (
    <TextField
      id={id}
      label={label}
      value={value ?? ""}
      required={required}
      autoFocus={autofocus}
      disabled={disabled}
      fullWidth
      size="small"
      onChange={(e) => onChange(e.target.value === "" ? options.emptyValue : e.target.value)}
      InputProps={{
        readOnly: readonly,
        endAdornment: inTauri() ? (
          <InputAdornment position="end">
            <Tooltip title={`Browse for ${directory ? "a folder" : "a file"}`}>
              <span>
                <IconButton edge="end" size="small" onClick={browse} disabled={!editable}
                  aria-label={`Browse for ${directory ? "a folder" : "a file"}`}>
                  <FolderOpenIcon fontSize="small" />
                </IconButton>
              </span>
            </Tooltip>
          </InputAdornment>
        ) : undefined,
      }}
    />
  );
}
