import { useState } from "react";
import { IconButton, InputAdornment, TextField, Tooltip } from "@mui/material";
import Visibility from "@mui/icons-material/Visibility";
import VisibilityOff from "@mui/icons-material/VisibilityOff";
import type { WidgetProps } from "@rjsf/utils";
import { useTranslation } from "react-i18next";

// rjsf widget for secret fields (#94): a password input with a show/hide toggle.
// Registered under the `password` key, so any field with `ui:widget: "password"`
// (or json-schema `format: "password"`) uses it.
//
// Never echoes a stored secret: the backend masks saved secrets to "***" before
// they reach the form (see admin/app.py), so the value here is the mask, not the
// real token. Revealing it shows the mask; submitting it unchanged is a no-op
// server-side.
export function PasswordWidget(props: WidgetProps) {
  const { id, value, onChange, label, required, disabled, readonly, autofocus, options, onBlur, onFocus } = props;
  const { t } = useTranslation();
  const [show, setShow] = useState(false);

  const handle = (v: string) => onChange(v === "" ? options.emptyValue : v);
  return (
    <TextField
      id={id}
      label={label}
      value={value ?? ""}
      type={show ? "text" : "password"}
      required={required}
      autoFocus={autofocus}
      disabled={disabled}
      fullWidth
      size="small"
      onChange={(e) => handle(e.target.value)}
      onBlur={(e) => onBlur?.(id, e.target.value)}
      onFocus={(e) => onFocus?.(id, e.target.value)}
      InputProps={{
        readOnly: readonly,
        // Mono so masked/token characters stay aligned (tabular).
        sx: { "& input": { fontFamily: '"Fira Code","Noto Sans SC",ui-monospace,monospace' } },
        endAdornment: (
          <InputAdornment position="end">
            <Tooltip title={show ? t("common.hide") : t("common.show")}>
              <IconButton
                edge="end"
                size="small"
                onClick={() => setShow((s) => !s)}
                aria-label={show ? t("common.hide") : t("common.show")}
                disabled={disabled}
              >
                {show ? <VisibilityOff fontSize="small" /> : <Visibility fontSize="small" />}
              </IconButton>
            </Tooltip>
          </InputAdornment>
        ),
      }}
    />
  );
}
