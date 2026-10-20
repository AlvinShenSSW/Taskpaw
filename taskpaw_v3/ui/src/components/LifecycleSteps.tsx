import {
  Box, CircularProgress, LinearProgress, Step as MuiStep, StepLabel, Stepper, Tooltip, Typography,
} from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ErrorIcon from "@mui/icons-material/Error";
import HourglassTopIcon from "@mui/icons-material/HourglassTop";
import RadioButtonUncheckedIcon from "@mui/icons-material/RadioButtonUnchecked";
import { useTranslation } from "react-i18next";
import type { WorkflowStep } from "../api";
import { currentStep, stepPercent, stepProgressDetail, useMediaLabels, type MediaLabels } from "./mediaWorkflow.helpers";

// H2 R5: the lifecycle 入库扫描 → 修复 → 识别 → 翻译 → 回传 → 整理 → 待确认 → 归档 → 清理 → 完成,
// as a compact line in each list row and as a vertical stepper in the film drawer.

function StateIcon({ step, size }: { step: WorkflowStep; size: number }) {
  const sx = { fontSize: size };
  switch (step.state) {
    case "done": return <CheckCircleIcon color="success" sx={sx} />;
    case "failed": return <ErrorIcon color="error" sx={sx} />;
    case "waiting": return <HourglassTopIcon color="warning" sx={sx} />;
    case "running": {
      const percent = stepPercent(step.progress);
      return percent === null
        ? <CircularProgress size={size - 2} thickness={6} />
        : <CircularProgress size={size - 2} thickness={6} variant="determinate" value={percent} />;
    }
    default: return <RadioButtonUncheckedIcon sx={{ ...sx, color: "text.disabled" }} />;
  }
}

// 「修复：进行中 40%（修复中）」 — the tooltip and accessible name of one step.
function describe(step: WorkflowStep, label: MediaLabels): string {
  const percent = stepPercent(step.progress);
  const extra = [label.note(step.note), stepProgressDetail(step.progress, label)].filter(Boolean).join(" · ");
  return `${label.step(step.key)}：${label.stepState(step.state)}${percent === null ? "" : ` ${percent}%`}`
    + (extra ? `（${extra}）` : "");
}

export function StepLine({ steps }: { steps: WorkflowStep[] }) {
  const { t } = useTranslation();
  const label = useMediaLabels();
  const current = currentStep(steps);
  return (
    <Box role="list" aria-label={t("media.lifecycle")}
      sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", rowGap: 0.5, columnGap: 0.25, mt: 0.5 }}>
      {steps.map((step, i) => {
        const percent = step.state === "running" ? stepPercent(step.progress) : null;
        const isCurrent = i === current;
        const text = describe(step, label);
        return (
          <Box key={step.key} sx={{ display: "flex", alignItems: "center" }}>
            {i > 0 && (
              <Typography component="span" aria-hidden variant="caption" sx={{ color: "text.disabled", px: 0.25 }}>›</Typography>
            )}
            <Tooltip title={text}>
              <Box role="listitem" aria-label={text} aria-current={isCurrent ? "step" : undefined}
                data-state={step.state}
                sx={{
                  display: "flex", alignItems: "center", gap: 0.25, px: 0.5, py: 0.125, borderRadius: 1,
                  border: 1, borderColor: isCurrent ? "primary.main" : "transparent",
                  bgcolor: isCurrent ? "action.selected" : "transparent",
                }}>
                <StateIcon step={step} size={14} />
                <Typography component="span" variant="caption" noWrap
                  sx={{
                    fontWeight: isCurrent ? 600 : 400,
                    color: step.state === "pending" ? "text.disabled"
                      : step.state === "failed" ? "error.main" : "text.primary",
                  }}>
                  {label.step(step.key)}{percent === null ? "" : ` ${percent}%`}
                </Typography>
              </Box>
            </Tooltip>
          </Box>
        );
      })}
    </Box>
  );
}

export function LifecycleStepper({ steps }: { steps: WorkflowStep[] }) {
  const { t } = useTranslation();
  const label = useMediaLabels();
  const current = currentStep(steps);
  return (
    <Stepper orientation="vertical" nonLinear activeStep={current} aria-label={t("media.lifecycle")}
      sx={{ "& .MuiStepConnector-line": { minHeight: 8 } }}>
      {steps.map((step, i) => {
        const percent = stepPercent(step.progress);
        const note = label.note(step.note);
        const detail = stepProgressDetail(step.progress, label);
        return (
          <MuiStep key={step.key} completed={step.state === "done"} aria-current={i === current ? "step" : undefined}>
            <StepLabel error={step.state === "failed"} icon={<StateIcon step={step} size={22} />}
              optional={
                <Box>
                  <Typography variant="caption" color={step.state === "pending" ? "text.disabled" : "text.secondary"}>
                    {label.stepState(step.state)}{percent === null ? "" : ` ${percent}%`}
                  </Typography>
                  {note && <Typography variant="body2" sx={{ wordBreak: "break-all" }}>{note}</Typography>}
                  {detail && (
                    <Typography variant="caption" color="text.secondary" component="div" sx={{ wordBreak: "break-all" }}>
                      {detail}
                    </Typography>
                  )}
                  {step.state === "running" && (
                    <LinearProgress sx={{ mt: 0.5, maxWidth: 320 }}
                      {...(percent === null ? {} : { variant: "determinate" as const, value: percent })} />
                  )}
                </Box>
              }>
              {label.step(step.key)}
            </StepLabel>
          </MuiStep>
        );
      })}
    </Stepper>
  );
}
