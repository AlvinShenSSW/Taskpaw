import { Alert, Button, Card, CardContent, Stack } from "@mui/material";
import { useState } from "react";
import { HttpError } from "../../api";
import { NetworkAccess } from "../../components/NetworkAccess";
import { afterNetwork, type StepProps } from "./model";

// #299 R4 (board Onboard-Network): the LAN switch is forced on for 主控 and GPU 工作机.
const NEXT: Record<string, string> = { controller: "下一步：主控设置", worker: "下一步：工作机设置", agent: "下一步：完成" };

export function NetworkStep({ role, advance, go }: StepProps) {
  const [error, setError] = useState<string | null>(null);
  return (
    <Card><CardContent>
      <Stack spacing={2}>
        {error && <Alert severity="warning" role="alert">{error}</Alert>}
        <NetworkAccess forceLan={role !== "agent"} wizard submitLabel={NEXT[role]}
          onSaved={() => {
            setError(null);
            advance(afterNetwork(role), role).catch((e) => setError(e instanceof HttpError || e instanceof Error ? e.message : String(e)));
          }}
          back={<Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} onClick={() => go("role")}>上一步</Button>} />
      </Stack>
    </CardContent></Card>
  );
}
