export type UploadFeedback = {
  id: string;
  projectId: string;
  assetId?: string;
  name: string;
  sizeBytes: number;
  progress: number;
  status: "uploading" | "available" | "failed";
  error?: string;
  exiting?: boolean;
};

export type PollableAsset = {
  asset_id: string;
  name: string;
  status: string;
  error?: string;
};

type PollResponse<TAsset extends PollableAsset> = {
  assets: TAsset[];
};

type PollUploadedAssetsOptions<TAsset extends PollableAsset> = {
  assetIds: readonly string[];
  request: (signal: AbortSignal) => Promise<PollResponse<TAsset>>;
  signal?: AbortSignal;
  timeoutMs?: number;
  intervalMs?: number;
};

export type RefreshRequestToken = Readonly<{
  projectId: string;
  requestId: number;
}>;

export type RefreshRequestGuard = {
  activateProject: (projectId: string) => void;
  begin: (projectId: string) => RefreshRequestToken;
  invalidate: (projectId: string) => void;
  canCommit: (token: RefreshRequestToken) => boolean;
};

export type AbortControllerRegistry = {
  create: () => AbortController;
  release: (controller: AbortController) => void;
  abortAll: (reason?: Error) => void;
  activeCount: () => number;
};

export class HttpError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "HttpError";
    this.status = status;
  }
}

export class UploadPollingTimeoutError extends Error {
  constructor() {
    super("文件仍在处理，未能确认已完成索引。请稍后在资料库中查看状态。");
    this.name = "UploadPollingTimeoutError";
  }
}

function abortReason(signal: AbortSignal): Error {
  if (signal.reason instanceof Error) return signal.reason;
  return new DOMException("操作已取消。", "AbortError");
}

function isRetryablePollingError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  if (!(error instanceof HttpError)) return false;
  return (
    error.status >= 500 ||
    error.status === 408 ||
    error.status === 425 ||
    error.status === 429
  );
}

async function runPollAttempt<TAsset extends PollableAsset>(
  request: (signal: AbortSignal) => Promise<PollResponse<TAsset>>,
  remainingMs: number,
  outerSignal?: AbortSignal,
): Promise<PollResponse<TAsset>> {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort(abortReason(outerSignal!));
  if (outerSignal?.aborted) forwardAbort();
  else outerSignal?.addEventListener("abort", forwardAbort, { once: true });

  const timer = globalThis.setTimeout(
    () => controller.abort(new UploadPollingTimeoutError()),
    remainingMs,
  );
  let onAttemptAbort = () => {};
  const aborted = new Promise<never>((_, reject) => {
    onAttemptAbort = () => reject(abortReason(controller.signal));
    if (controller.signal.aborted) onAttemptAbort();
    else {
      controller.signal.addEventListener(
        "abort",
        onAttemptAbort,
        { once: true },
      );
    }
  });

  try {
    return await Promise.race([request(controller.signal), aborted]);
  } finally {
    globalThis.clearTimeout(timer);
    controller.signal.removeEventListener("abort", onAttemptAbort);
    outerSignal?.removeEventListener("abort", forwardAbort);
  }
}

function waitForNextPoll(
  intervalMs: number,
  deadline: number,
  signal?: AbortSignal,
): Promise<void> {
  const remainingMs = deadline - Date.now();
  if (remainingMs <= 0) return Promise.resolve();
  const delayMs = Math.min(intervalMs, remainingMs);
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      globalThis.clearTimeout(timer);
      reject(abortReason(signal!));
    };
    const timer = globalThis.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    if (signal?.aborted) onAbort();
    else signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function pollUploadedAssets<TAsset extends PollableAsset>({
  assetIds,
  request,
  signal,
  timeoutMs = 90_000,
  intervalMs = 500,
}: PollUploadedAssetsOptions<TAsset>): Promise<{
  assets: TAsset[];
  allAssets: TAsset[];
}> {
  const ids = new Set(assetIds);
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    if (signal?.aborted) throw abortReason(signal);
    const remainingMs = deadline - Date.now();
    let data: PollResponse<TAsset>;
    try {
      data = await runPollAttempt(request, remainingMs, signal);
    } catch (error) {
      if (signal?.aborted) throw abortReason(signal);
      if (
        error instanceof UploadPollingTimeoutError ||
        Date.now() >= deadline
      ) {
        throw new UploadPollingTimeoutError();
      }
      if (!isRetryablePollingError(error)) throw error;
      await waitForNextPoll(intervalMs, deadline, signal);
      continue;
    }

    const selected = data.assets.filter((asset) => ids.has(asset.asset_id));
    const failed = selected.find((asset) => asset.status === "failed");
    if (failed) {
      throw new Error(
        `${failed.name} 处理失败：${failed.error || "请检查文件内容。"}`,
      );
    }
    if (
      selected.length === ids.size &&
      selected.every((asset) => asset.status === "ready")
    ) {
      return { assets: selected, allAssets: data.assets };
    }
    await waitForNextPoll(intervalMs, deadline, signal);
  }

  throw new UploadPollingTimeoutError();
}

export function markUploadAvailable(item: UploadFeedback): UploadFeedback {
  return { ...item, status: "available", progress: 100 };
}

export function removeUploadFeedback(
  items: readonly UploadFeedback[],
  feedbackId: string,
): UploadFeedback[] {
  return items.filter((item) => item.id !== feedbackId);
}

export function finalizeUploadOperationFeedback(
  items: UploadFeedback[],
  operationFeedbackIds: readonly string[],
  cancelled: boolean,
): UploadFeedback[] {
  if (!cancelled) return items;
  const ids = new Set(operationFeedbackIds);
  return items.filter((item) => !ids.has(item.id));
}

export function canChangeProjectDuringBusy(
  busy: boolean,
  hasCancellableOperation: boolean,
): boolean {
  return !busy || hasCancellableOperation;
}

export function visibleAssetsWithoutActiveFeedback<
  TAsset extends { asset_id: string },
>(assets: readonly TAsset[], feedback: readonly UploadFeedback[]): TAsset[] {
  const feedbackAssetIds = new Set(
    feedback.flatMap((item) => (item.assetId ? [item.assetId] : [])),
  );
  return assets.filter((asset) => !feedbackAssetIds.has(asset.asset_id));
}

export function createRefreshRequestGuard(
  initialProjectId: string,
): RefreshRequestGuard {
  let activeProjectId = initialProjectId;
  const latestRequestIds = new Map<string, number>();

  return {
    activateProject(projectId) {
      activeProjectId = projectId;
    },
    begin(projectId) {
      const requestId = (latestRequestIds.get(projectId) || 0) + 1;
      latestRequestIds.set(projectId, requestId);
      return { projectId, requestId };
    },
    invalidate(projectId) {
      latestRequestIds.set(
        projectId,
        (latestRequestIds.get(projectId) || 0) + 1,
      );
    },
    canCommit(token) {
      return (
        token.projectId === activeProjectId &&
        latestRequestIds.get(token.projectId) === token.requestId
      );
    },
  };
}

export async function continueProjectWorkflow<T>(
  guard: RefreshRequestGuard,
  token: RefreshRequestToken,
  load: () => Promise<T>,
): Promise<
  | { active: true; value: T }
  | { active: false }
> {
  const value = await load();
  if (!guard.canCommit(token)) return { active: false };
  return { active: true, value };
}

export function createAbortControllerRegistry(): AbortControllerRegistry {
  const controllers = new Set<AbortController>();

  return {
    create() {
      const controller = new AbortController();
      controllers.add(controller);
      return controller;
    },
    release(controller) {
      controllers.delete(controller);
    },
    abortAll(reason = new DOMException("操作已取消。", "AbortError")) {
      controllers.forEach((controller) => controller.abort(reason));
      controllers.clear();
    },
    activeCount() {
      return controllers.size;
    },
  };
}

export function beginConversationNavigation({
  workflowGuard,
  conversationGuard,
  controllers,
  projectId,
}: {
  workflowGuard: RefreshRequestGuard;
  conversationGuard: RefreshRequestGuard;
  controllers: AbortControllerRegistry;
  projectId: string;
}): RefreshRequestToken {
  workflowGuard.invalidate(projectId);
  controllers.abortAll();
  return conversationGuard.begin(projectId);
}

export function beginConversationDeletion({
  conversationGuard,
  workflowGuard,
  controllers,
  projectId,
  isCurrent,
}: {
  conversationGuard: RefreshRequestGuard;
  workflowGuard: RefreshRequestGuard;
  controllers: AbortControllerRegistry;
  projectId: string;
  isCurrent: boolean;
}): RefreshRequestToken {
  if (isCurrent) {
    workflowGuard.invalidate(projectId);
    controllers.abortAll();
  }
  conversationGuard.invalidate(projectId);
  return conversationGuard.begin(projectId);
}

export function canApplyConversationDeletionFallback({
  conversationGuard,
  deletionToken,
  targetId,
  wasCurrentAtDelete,
  currentConversationId,
}: {
  conversationGuard: RefreshRequestGuard;
  deletionToken: RefreshRequestToken;
  targetId: string;
  wasCurrentAtDelete: boolean;
  currentConversationId?: string | null;
}): boolean {
  return (
    wasCurrentAtDelete &&
    currentConversationId === targetId &&
    conversationGuard.canCommit(deletionToken)
  );
}

export async function commitLatestRefresh<T>(
  guard: RefreshRequestGuard,
  token: RefreshRequestToken,
  load: () => Promise<T>,
  commit: (value: T) => void,
): Promise<T | null> {
  const value = await load();
  if (!guard.canCommit(token)) return null;
  commit(value);
  return value;
}

export async function commitLatestProjectResponse<T>(
  guard: RefreshRequestGuard,
  token: RefreshRequestToken,
  load: () => Promise<T>,
  projectIdOf: (value: T) => string,
  commit: (value: T) => void,
): Promise<T | null> {
  const value = await load();
  if (
    !guard.canCommit(token) ||
    projectIdOf(value) !== token.projectId
  ) {
    return null;
  }
  commit(value);
  return value;
}
