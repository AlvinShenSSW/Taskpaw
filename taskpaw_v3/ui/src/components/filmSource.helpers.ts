import { FilmRequestError } from "../api";

export type FilmSource = { kind: "agent" } | { kind: "hub"; serverId: number };
export const AGENT_SOURCE: FilmSource = { kind: "agent" };

export function filmQueryPrefix(resource: "films" | "runFilms", name: string, source: FilmSource) {
  return source.kind === "hub"
    ? [resource === "films" ? "hubFilms" : "hubRunFilms", source.serverId, name]
    : [resource, name];
}

export type FilmNoteKey = `hub.films.${"loading" | "unavailable" | "offline" | "disabled" | "unknown"
  | "authFailed" | "hubAuthFailed" | "timeout" | "failed" | "stale" | "resyncing" | "noSnapshot" | "empty"}`;
export type FilmFailure = { note: FilmNoteKey; definite: boolean };

export function filmFailure(error: unknown): FilmFailure {
  const notes: Record<string, FilmNoteKey> = {
    unknown_server: "hub.films.unknown", agent_disabled: "hub.films.disabled",
    agent_offline: "hub.films.offline", film_list_unavailable: "hub.films.unavailable",
    agent_auth_failed: "hub.films.authFailed", agent_timeout: "hub.films.timeout",
  };
  if (!(error instanceof FilmRequestError)) return { note: "hub.films.failed", definite: false };
  return {
    note: error.status === 401 ? "hub.films.hubAuthFailed" : notes[error.code] ?? "hub.films.failed",
    definite: [404, 409, 503].includes(error.status),
  };
}
