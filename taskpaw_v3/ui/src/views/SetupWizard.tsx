import { Box, Stack, Step, StepLabel, Stepper, Typography } from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type SetupRole, type SetupState, type SetupStep } from "../api";
import { ControllerStep } from "./setup/ControllerStep";
import { DoneStep } from "./setup/DoneStep";
import { NetworkStep } from "./setup/NetworkStep";
import { RoleStep } from "./setup/RoleStep";
import { WorkerFieldsStep } from "./setup/WorkerFieldsStep";
import { WorkerJoinStep } from "./setup/WorkerJoinStep";
import { AgentConsole } from "./AgentConsole";
import { SETUP_KEY, stepsOf, titleOf, type StepProps } from "./setup/model";

// #299 R2, §6, §7 (boards Main, Onboard-*): the first-run wizard. 普通 Agent role → network → done;
// 主控 role → network → controller → done; 工作机 role → network → worker_fields → worker_join → done.
// Each 下一步 saves the step through the real writers, then records the next step (PUT /control/setup);
// 上一步 / 修改 go back without writing. After any restart the wizard resumes at the saved step and
// re-reads every form from the saved config.
export function SetupWizard({ state }: { state: SetupState }) {
  const qc = useQueryClient();
  const [role, setRole] = useState<SetupRole | null>(state.role);
  const [step, setStep] = useState<SetupStep>(state.step ?? "role");
  // a refusal propagates to the step, which shows it next to its own fields
  const advance = async (next: SetupStep, as: SetupRole) => {
    await api.setupStep(next, as);
    setRole(as);
    setStep(next);
    qc.invalidateQueries({ queryKey: SETUP_KEY });
  };
  const props: StepProps = { role: role ?? "agent", advance, go: setStep };
  const steps = stepsOf(role);
  const active = Math.max(0, steps.findIndex(([keys]) => keys.includes(step)));
  return (
    <Stack spacing={2.5} sx={{ maxWidth: 960, mx: "auto", width: "100%" }}>
      <Stack direction="row" justifyContent="space-between" alignItems="baseline" flexWrap="wrap" useFlexGap spacing={1}>
        <Typography variant="h5" component="h1">{titleOf(step, role)}</Typography>
        {step === "role" && <Typography variant="body2" color="text.secondary">之后可在 设置 → 媒体工作流 更改</Typography>}
      </Stack>
      <Box sx={{ overflowX: "auto" }}>
        <Stepper activeStep={active} alternativeLabel sx={{ minWidth: 320 }}>
          {steps.map(([, label]) => <Step key={label}><StepLabel>{label}</StepLabel></Step>)}
        </Stepper>
      </Box>
      {step === "role" ? <RoleStep {...props} initial={role} />
        : step === "network" ? <NetworkStep {...props} />
          : step === "controller" ? <ControllerStep {...props} />
            : step === "worker_fields" ? <WorkerFieldsStep {...props} />
              : step === "worker_join" ? <WorkerJoinStep {...props} />
                : <DoneStep {...props} />}
    </Stack>
  );
}

/** R2: the agent shows the wizard while the first-run marker is pending; absent / done (or an older
 * backend without /control/setup) → the console as today. */
export function AgentHome() {
  const setup = useQuery({ queryKey: SETUP_KEY, queryFn: api.setup, retry: false });
  if (setup.data?.status === "pending") return <SetupWizard state={setup.data} />;
  return <AgentConsole />;
}
