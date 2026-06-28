import { InputAdornment, TextField, Tooltip } from "@mui/material";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import IconButton from "@mui/material/IconButton";
import type { WidgetProps } from "@rjsf/utils";

// rjsf widget for path fields (#71): a text input + a Browse button that opens
// the NATIVE Tauri file/directory picker and writes the chosen absolute path back
// into the form. The field's `ui:options.taskpawPath` ("file" | "directory")
// chooses the dialog mode. Outside the Tauri shell (browser/dev) the Browse
// button is hidden and it degrades to a plain text input — a web <input
// type=file> can't expose a real filesystem path anyway.

// The locked shell injects __TAURI_INTERNALS__ on the webview; its absence means
// we're in a browser (Vite dev / web build) with no native dialog.
function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function PathWidget(props: WidgetProps) {
  const { id, value, onChange, label, options, disabled, readonly, required, autofocus } = props;
  const directory = (options?.taskpawPath as string) === "directory";

  const browse = async () => {
    try {
      // Imported lazily so the web build never needs the Tauri plugin at load.
      const { open } = await import("@tauri-apps/plugin-dialog");
      const picked = await open({
        directory,
        multiple: false,
        title: `Select ${directory ? "folder" : "file"}${label ? ` — ${label}` : ""}`,
      });
      if (typeof picked === "string") onChange(picked);
    } catch {
      // Dialog unavailable/cancelled — leave the typed value untouched.
    }
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
