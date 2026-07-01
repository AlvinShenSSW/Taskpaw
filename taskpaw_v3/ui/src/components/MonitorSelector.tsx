import { Button, Chip, Stack } from "@mui/material";
import { useTranslation } from "react-i18next";
import { StatusDot } from "./StatusDot";
import type { MonitorSnapshot } from "../api";

// Horizontal segmented selector for the multi-monitor agent console (design
// pages/agent-console.md, case B): a row of pills (each = status dot + name +
// type chip) plus a trailing "+ Add monitor" pill, replacing the tall left rail.
// The selected pill is highlighted (accent border + wash) AND carries
// aria-current, so selection is never conveyed by color alone (a11y §1).
export function MonitorSelector({
  names, monitors, selected, onSelect, onAdd,
}: {
  names: string[];
  monitors: Record<string, MonitorSnapshot>;
  selected?: string;
  onSelect: (name: string) => void;
  onAdd: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: "wrap", rowGap: 1 }}
      aria-label={t("agent.monitors")}>
      {names.map((n) => {
        const m = monitors[n];
        const on = n === selected;
        return (
          <Button
            key={n}
            onClick={() => onSelect(n)}
            aria-current={on ? "true" : undefined}
            variant="outlined"
            size="small"
            startIcon={<StatusDot state={m.state} />}
            sx={{
              textTransform: "none",
              borderColor: on ? "primary.main" : "divider",
              bgcolor: on ? "rgba(34,197,94,.08)" : "transparent",
              "&:hover": { bgcolor: on ? "rgba(34,197,94,.13)" : "action.hover" },
            }}
          >
            {n}
            {m.type_id && <Chip size="small" label={m.type_id} sx={{ ml: 0.75 }} />}
          </Button>
        );
      })}
      <Button onClick={onAdd} variant="text" size="small" sx={{ textTransform: "none" }}>
        + {t("agent.addMonitor")}
      </Button>
    </Stack>
  );
}
