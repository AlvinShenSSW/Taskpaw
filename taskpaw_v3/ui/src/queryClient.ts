import { QueryClient } from "@tanstack/react-query";
import { ControlCredentialError } from "./api";

// #379: no default refetchInterval. Live queries declare their own cadence; static ones
// refresh on mount, window refocus and mutation invalidation.
export const createQueryClient = () => new QueryClient({
  defaultOptions: { queries: { retry: (count, error) => !(error instanceof ControlCredentialError) && count < 1 } },
});
