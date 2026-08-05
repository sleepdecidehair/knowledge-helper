import type { Options } from "@anthropic-ai/claude-agent-sdk";

export type SessionQueryRun = {
  terminal?: Record<string, unknown>;
  streamError?: string;
  contextUsage?: Record<string, unknown>;
};

type QueryExecutor = (prompt: string, options: Options) => Promise<SessionQueryRun>;

export function queryFailure(run: SessionQueryRun): string | undefined {
  const errors = Array.isArray(run.terminal?.errors)
    ? run.terminal.errors.filter((item): item is string => typeof item === "string")
    : [];
  return errors[0] ?? run.streamError;
}

export function isMissingSessionError(
  error: string | undefined,
  sessionId: string | undefined | null,
): boolean {
  if (!error || !sessionId) return false;
  return error.trim() === `No conversation found with session ID: ${sessionId}`;
}

export function withoutSessionResume(options: Options): Options {
  const { resume: _resume, forkSession: _forkSession, ...freshOptions } = options;
  return freshOptions;
}

export function resolveCompactRun(
  run: SessionQueryRun,
  options: Options,
  sessionId: string | undefined | null,
):
  | { kind: "recovered"; options: Options }
  | { kind: "resumed"; options: Options }
  | { kind: "failed"; error: string; options: Options }
  | { kind: "unavailable"; options: Options } {
  const failure = queryFailure(run);
  if (isMissingSessionError(failure, sessionId)) {
    return { kind: "recovered", options: withoutSessionResume(options) };
  }
  if (failure) {
    return { kind: "failed", error: failure, options };
  }
  const compactSessionId = typeof run.terminal?.session_id === "string"
    ? run.terminal.session_id
    : undefined;
  if (run.terminal?.subtype === "success" && compactSessionId) {
    return {
      kind: "resumed",
      options: { ...options, resume: compactSessionId, forkSession: false },
    };
  }
  return { kind: "unavailable", options };
}

export async function runQuestionWithSessionRecovery(
  prompt: string,
  options: Options,
  sessionId: string | undefined | null,
  runQuery: QueryExecutor,
  onRecovered: () => void = () => undefined,
): Promise<{ run: SessionQueryRun; activeOptions: Options; recovered: boolean }> {
  let activeOptions = options;
  let run = await runQuery(prompt, activeOptions);
  if (!isMissingSessionError(queryFailure(run), sessionId)) {
    return { run, activeOptions, recovered: false };
  }

  activeOptions = withoutSessionResume(options);
  onRecovered();
  run = await runQuery(prompt, activeOptions);
  return { run, activeOptions, recovered: true };
}
