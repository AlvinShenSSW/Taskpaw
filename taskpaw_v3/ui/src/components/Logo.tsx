import { Box } from "@mui/material";
import logoUrl from "../assets/logo.png";

// The project brand mark (#120): the TaskPaw paw + gauge logo (Logo/logo.png,
// downscaled to 256px for the bundle). Decorative in the top bar — the adjacent
// "TaskPaw" wordmark carries the accessible name, so the image is aria-hidden
// with an empty alt. Callers that show it standalone (About) pass an alt.
export function Logo({ size = 22, alt = "" }: { size?: number; alt?: string }) {
  return (
    <Box
      component="img"
      src={logoUrl}
      alt={alt}
      aria-hidden={alt ? undefined : true}
      width={size}
      height={size}
      sx={{ display: "inline-block", borderRadius: `${Math.round(size * 0.22)}px` }}
    />
  );
}
