import { EmptyState, PageTitle } from "./parts";

// Mac 信息桥 (§4.6) stays in the rail marked 「后续」.
export function LaterPage() {
  return (
    <>
      <PageTitle title="Mac 信息桥" />
      <EmptyState title="此功能将在多机工作流阶段提供" description="Mac 上只读的状态、事件与有界脱敏日志，不参与派单。" />
    </>
  );
}
