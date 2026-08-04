import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import ts from "typescript";

const sourcePath = new URL("../src/App.tsx", import.meta.url);
const stylePath = new URL("../src/App.css", import.meta.url);
const behaviorPath = new URL("../src/uploadFeedback.ts", import.meta.url);

async function loadBehaviorModule() {
  const source = await readFile(behaviorPath, "utf8");
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: "uploadFeedback.ts",
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "上传反馈行为模块应能由 TypeScript 转译");
  return import(
    `data:text/javascript;base64,${Buffer.from(transpiled.outputText).toString("base64")}`
  );
}

test("单次永不返回的轮询请求受总体 deadline 中止", async () => {
  const { UploadPollingTimeoutError, pollUploadedAssets } =
    await loadBehaviorModule();
  let requestSignal;
  const startedAt = Date.now();

  await assert.rejects(
    pollUploadedAssets({
      assetIds: ["asset-1"],
      timeoutMs: 30,
      intervalMs: 1,
      request: (signal) => {
        requestSignal = signal;
        return new Promise(() => {});
      },
    }),
    UploadPollingTimeoutError,
  );

  assert.equal(requestSignal?.aborted, true);
  assert.ok(Date.now() - startedAt < 250, "挂起请求不应越过总体 deadline");
});

test("服务端 ready 后反馈才切换为 100% 和可用", async () => {
  const { markUploadAvailable, pollUploadedAssets } =
    await loadBehaviorModule();
  const readyAsset = {
    asset_id: "asset-ready",
    name: "guide.md",
    status: "ready",
  };

  const result = await pollUploadedAssets({
    assetIds: [readyAsset.asset_id],
    timeoutMs: 100,
    intervalMs: 1,
    request: async () => ({ assets: [readyAsset] }),
  });
  const feedback = markUploadAvailable({
    id: "feedback-ready",
    projectId: "project-a",
    assetId: readyAsset.asset_id,
    name: readyAsset.name,
    sizeBytes: 10,
    progress: 92,
    status: "uploading",
  });

  assert.deepEqual(result.assets, [readyAsset]);
  assert.equal(feedback.status, "available");
  assert.equal(feedback.progress, 100);
});

test("terminal 4xx 立即失败而网络和可重试服务错误继续轮询", async () => {
  const { HttpError, pollUploadedAssets } = await loadBehaviorModule();
  let terminalAttempts = 0;
  await assert.rejects(
    pollUploadedAssets({
      assetIds: ["asset-terminal"],
      timeoutMs: 100,
      intervalMs: 1,
      request: async () => {
        terminalAttempts += 1;
        throw new HttpError(422, "文件参数无效");
      },
    }),
    (error) => error instanceof HttpError && error.status === 422,
  );
  assert.equal(terminalAttempts, 1);

  let retryableAttempts = 0;
  const result = await pollUploadedAssets({
    assetIds: ["asset-retry"],
    timeoutMs: 200,
    intervalMs: 1,
    request: async () => {
      retryableAttempts += 1;
      if (retryableAttempts === 1) throw new TypeError("network unavailable");
      if (retryableAttempts === 2) throw new HttpError(503, "service unavailable");
      return {
        assets: [
          { asset_id: "asset-retry", name: "retry.md", status: "ready" },
        ],
      };
    },
  });
  assert.equal(retryableAttempts, 3);
  assert.equal(result.assets[0].status, "ready");
});

test("组件 teardown signal 会中止活动轮询", async () => {
  const { pollUploadedAssets } = await loadBehaviorModule();
  const teardown = new AbortController();
  let requestSignal;
  const polling = pollUploadedAssets({
    assetIds: ["asset-teardown"],
    timeoutMs: 5_000,
    intervalMs: 1,
    signal: teardown.signal,
    request: (signal) => {
      requestSignal = signal;
      return new Promise(() => {});
    },
  });

  teardown.abort();

  await assert.rejects(polling, (error) => error?.name === "AbortError");
  assert.equal(requestSignal?.aborted, true);
});

test("乱序项目 refresh 只有活动项目的最新 request token 可以提交", async () => {
  const { commitLatestRefresh, createRefreshRequestGuard } =
    await loadBehaviorModule();
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => {
      resolve = done;
    });
    return { promise, resolve };
  };
  const guard = createRefreshRequestGuard("project-a");
  const oldProject = deferred();
  const oldProjectToken = guard.begin("project-a");
  const commits = [];
  const oldProjectRefresh = commitLatestRefresh(
    guard,
    oldProjectToken,
    () => oldProject.promise,
    (value) => commits.push(value),
  );

  guard.activateProject("project-b");
  const olderActive = deferred();
  const olderActiveToken = guard.begin("project-b");
  const olderActiveRefresh = commitLatestRefresh(
    guard,
    olderActiveToken,
    () => olderActive.promise,
    (value) => commits.push(value),
  );
  const latestActive = deferred();
  const latestActiveToken = guard.begin("project-b");
  const latestActiveRefresh = commitLatestRefresh(
    guard,
    latestActiveToken,
    () => latestActive.promise,
    (value) => commits.push(value),
  );

  latestActive.resolve("latest-project-b");
  assert.equal(await latestActiveRefresh, "latest-project-b");
  olderActive.resolve("older-project-b");
  oldProject.resolve("old-project-a");
  assert.equal(await olderActiveRefresh, null);
  assert.equal(await oldProjectRefresh, null);
  assert.deepEqual(commits, ["latest-project-b"]);
});

test("refresh 后的乱序 conversation 响应不能回写旧项目", async () => {
  const { commitLatestProjectResponse, createRefreshRequestGuard } =
    await loadBehaviorModule();
  const deferred = () => {
    let resolve;
    const promise = new Promise((done) => {
      resolve = done;
    });
    return { promise, resolve };
  };
  const guard = createRefreshRequestGuard("project-a");
  const projectA = deferred();
  const tokenA = guard.begin("project-a");
  const commits = [];
  const conversationA = commitLatestProjectResponse(
    guard,
    tokenA,
    () => projectA.promise,
    (conversation) => conversation.project_id,
    (conversation) => commits.push(conversation.id),
  );

  guard.activateProject("project-b");
  const projectB = deferred();
  const tokenB = guard.begin("project-b");
  const conversationB = commitLatestProjectResponse(
    guard,
    tokenB,
    () => projectB.promise,
    (conversation) => conversation.project_id,
    (conversation) => commits.push(conversation.id),
  );

  guard.activateProject("project-c");
  const projectC = deferred();
  const tokenC = guard.begin("project-c");
  const conversationC = commitLatestProjectResponse(
    guard,
    tokenC,
    () => projectC.promise,
    (conversation) => conversation.project_id,
    (conversation) => commits.push(conversation.id),
  );

  projectC.resolve({ id: "conversation-c", project_id: "project-c" });
  assert.equal((await conversationC).id, "conversation-c");
  projectB.resolve({ id: "conversation-b", project_id: "project-b" });
  projectA.resolve({ id: "conversation-a", project_id: "project-a" });
  assert.equal(await conversationB, null);
  assert.equal(await conversationA, null);
  assert.deepEqual(commits, ["conversation-c"]);

  const mismatchedToken = guard.begin("project-c");
  const mismatched = await commitLatestProjectResponse(
    guard,
    mismatchedToken,
    async () => ({ id: "wrong-project", project_id: "project-a" }),
    (conversation) => conversation.project_id,
    (conversation) => commits.push(conversation.id),
  );
  assert.equal(mismatched, null);
});

test("新建会话会失效同项目中尚未返回的 conversation 请求", async () => {
  const { commitLatestProjectResponse, createRefreshRequestGuard } =
    await loadBehaviorModule();
  let resolveConversation;
  const pendingConversation = new Promise((resolve) => {
    resolveConversation = resolve;
  });
  const guard = createRefreshRequestGuard("project-a");
  const token = guard.begin("project-a");
  const commits = [];
  const opening = commitLatestProjectResponse(
    guard,
    token,
    () => pendingConversation,
    (conversation) => conversation.project_id,
    (conversation) => commits.push(conversation.id),
  );

  guard.invalidate("project-a");
  resolveConversation({ id: "old-conversation", project_id: "project-a" });

  assert.equal(await opening, null);
  assert.deepEqual(commits, []);
});

test("项目工作流在切换项目后的首个异步步骤返回时立即停止提交", async () => {
  const { continueProjectWorkflow, createRefreshRequestGuard } =
    await loadBehaviorModule();
  let resolveCreate;
  const createResponse = new Promise((resolve) => {
    resolveCreate = resolve;
  });
  const guard = createRefreshRequestGuard("project-a");
  const token = guard.begin("project-a");
  const commits = [];
  const workflow = (async () => {
    const created = await continueProjectWorkflow(
      guard,
      token,
      () => createResponse,
    );
    if (!created.active) return "stale";
    commits.push(created.value.id);
    return "committed";
  })();

  guard.activateProject("project-b");
  resolveCreate({ id: "conversation-a", project_id: "project-a" });

  assert.equal(await workflow, "stale");
  assert.deepEqual(commits, []);
});

test("删除会话会在 DELETE 前失效目标项目的延迟 GET", async () => {
  const {
    beginConversationDeletion,
    commitLatestProjectResponse,
    createAbortControllerRegistry,
    createRefreshRequestGuard,
  } =
    await loadBehaviorModule();
  let resolveConversation;
  const pendingConversation = new Promise((resolve) => {
    resolveConversation = resolve;
  });
  const guard = createRefreshRequestGuard("project-a");
  const workflowGuard = createRefreshRequestGuard("project-a");
  const controllers = createAbortControllerRegistry();
  const workflowToken = workflowGuard.begin("project-a");
  const workflowController = controllers.create();
  const token = guard.begin("project-a");
  const commits = [];
  const opening = commitLatestProjectResponse(
    guard,
    token,
    () => pendingConversation,
    (conversation) => conversation.project_id,
    (conversation) => commits.push(conversation.id),
  );

  const deleteEvents = [];
  const deletionToken = beginConversationDeletion({
    conversationGuard: guard,
    workflowGuard,
    controllers,
    projectId: "project-a",
    isCurrent: true,
  });
  deleteEvents.push("invalidated");
  await Promise.resolve().then(() => deleteEvents.push("deleted"));
  resolveConversation({ id: "conversation-x", project_id: "project-a" });

  assert.deepEqual(deleteEvents, ["invalidated", "deleted"]);
  assert.equal(await opening, null);
  assert.deepEqual(commits, []);
  assert.equal(guard.canCommit(deletionToken), true);
  assert.equal(workflowGuard.canCommit(workflowToken), false);
  assert.equal(workflowController.signal.aborted, true);
  assert.equal(controllers.activeCount(), 0);

  guard.begin("project-a");
  assert.equal(
    guard.canCommit(deletionToken),
    false,
    "删除等待期间的新会话导航必须阻止旧 fallback",
  );
});

test("用户会话导航统一取消旧工作流并取得最新 conversation token", async () => {
  const {
    beginConversationNavigation,
    createAbortControllerRegistry,
    createRefreshRequestGuard,
  } = await loadBehaviorModule();
  const workflowGuard = createRefreshRequestGuard("project-a");
  const conversationGuard = createRefreshRequestGuard("project-a");
  const controllers = createAbortControllerRegistry();
  const workflowToken = workflowGuard.begin("project-a");
  const controller = controllers.create();

  const navigationToken = beginConversationNavigation({
    workflowGuard,
    conversationGuard,
    controllers,
    projectId: "project-a",
  });

  assert.equal(workflowGuard.canCommit(workflowToken), false);
  assert.equal(controller.signal.aborted, true);
  assert.equal(controllers.activeCount(), 0);
  assert.equal(conversationGuard.canCommit(navigationToken), true);

  conversationGuard.begin("project-a");
  assert.equal(conversationGuard.canCommit(navigationToken), false);
});

test("整段上传 controller 会在 teardown 中止挂起 POST 并执行 finally 清理", async () => {
  const { createAbortControllerRegistry } = await loadBehaviorModule();
  const registry = createAbortControllerRegistry();
  const controller = registry.create();
  let postSignal;
  let progressActive = true;
  const upload = (async () => {
    try {
      await new Promise((resolve, reject) => {
        postSignal = controller.signal;
        const onAbort = () => reject(controller.signal.reason);
        if (controller.signal.aborted) onAbort();
        else controller.signal.addEventListener("abort", onAbort, { once: true });
      });
    } finally {
      progressActive = false;
      registry.release(controller);
    }
  })();

  registry.abortAll();

  await assert.rejects(upload, (error) => error?.name === "AbortError");
  assert.equal(postSignal?.aborted, true);
  assert.equal(progressActive, false);
  assert.equal(registry.activeCount(), 0);
});

test("失败反馈抑制重复卡片且成功反馈清理后才并入", async () => {
  const { removeUploadFeedback, visibleAssetsWithoutActiveFeedback } =
    await loadBehaviorModule();
  const assets = [
    { asset_id: "asset-ready", status: "ready" },
    { asset_id: "asset-failed", status: "failed" },
    { asset_id: "asset-existing", status: "ready" },
  ];
  const feedback = [
    {
      id: "feedback-ready",
      projectId: "project-a",
      assetId: "asset-ready",
      name: "ready.md",
      sizeBytes: 10,
      progress: 100,
      status: "available",
      exiting: true,
    },
    {
      id: "feedback-failed",
      projectId: "project-a",
      assetId: "asset-failed",
      name: "failed.md",
      sizeBytes: 20,
      progress: 72,
      status: "failed",
      error: "解析失败",
    },
  ];

  assert.deepEqual(
    visibleAssetsWithoutActiveFeedback(assets, feedback).map(
      (asset) => asset.asset_id,
    ),
    ["asset-existing"],
  );

  const afterSuccessfulAnimation = removeUploadFeedback(
    feedback,
    "feedback-ready",
  );
  assert.deepEqual(
    visibleAssetsWithoutActiveFeedback(
      assets,
      afterSuccessfulAnimation,
    ).map((asset) => asset.asset_id),
    ["asset-ready", "asset-existing"],
  );
  assert.equal(afterSuccessfulAnimation[0].id, "feedback-failed");
});

test("App 接入可执行上传反馈模块并保留展示契约", async () => {
  const source = await readFile(sourcePath, "utf8");

  assert.match(source, /from "\.\/uploadFeedback"/);
  assert.match(source, /size_bytes: number/);
  assert.match(source, /function formatFileSize\(sizeBytes: number\)/);
  assert.match(source, /progress: 8/);
  assert.match(source, /workflowGuardRef\.current\.activateProject\(id\)/);
  assert.match(source, /const navigationToken = workflowGuardRef\.current\.begin\(id\)/);
  assert.match(
    source,
    /continueProjectWorkflow\([\s\S]*?navigationToken,[\s\S]*?\(\) => refresh\(id, ""\)/,
  );
  assert.match(source, /continueProjectWorkflow\(/);
  assert.match(source, /function resetConversationState\(/);
  assert.match(source, /else resetConversationState\(id\)/);
  assert.match(source, /const initialNavigationToken = workflowGuardRef\.current\.begin\(/);
  assert.match(source, /function navigateToConversation\(/);
  assert.match(source, /void navigateToConversation\(item\.id, item\.project_id\)/);
  assert.match(
    source,
    /beginConversationDeletion\([\s\S]*?target\.project_id[\s\S]*?method: "DELETE"/,
  );
  assert.match(source, /conversationGuardRef\.current\.canCommit\(deletionToken\)/);
  assert.match(
    source,
    /uploadFilesToProject\([\s\S]*?uploadProjectId,[\s\S]*?operationController\.signal/,
  );
  assert.match(
    source,
    /waitForUploadedAssets\([\s\S]*?uploadProjectId,[\s\S]*?operationController\.signal/,
  );
  assert.match(
    source,
    /uploadFilesToProject\([\s\S]*?queuedFiles,[\s\S]*?requestProjectId,[\s\S]*?requestController\.signal/,
  );
  assert.match(source, /window\.clearInterval\(progressTimer\)/);
  assert.match(source, /上传中/);
  assert.match(source, /可用/);
  assert.match(source, /formatFileSize\(asset\.size_bytes\)/);
});

test("上传记录仅使用短 transform opacity 动效并支持减弱动效", async () => {
  const css = await readFile(stylePath, "utf8");

  assert.match(css, /\.upload-feedback-item[\s\S]*?animation: upload-feedback-enter 160ms ease-out/);
  assert.match(css, /\.upload-feedback-item\.is-exiting[\s\S]*?opacity: 0;[\s\S]*?transform: translateY\(-6px\)/);
  assert.match(css, /\.upload-feedback-progress > span[\s\S]*?transition: transform 180ms ease-out/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /\.upload-feedback-item[\s\S]*?\.asset-card\.is-new[\s\S]*?animation: none !important/);
  assert.match(css, /transform: none !important/);
});
