import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, ClipboardEvent, FormEvent } from "react";
import {
  Check,
  Copy,
  Download,
  Ellipsis,
  Eye,
  Paperclip,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Upload,
} from "lucide-react";
import {
  Button,
  Card,
  Chip,
  Dropdown,
  Input,
  ListBox,
  Modal,
  ProgressBar,
  Select,
  TextArea,
  useOverlayState,
} from "@heroui/react";
import { Sidebar } from "@heroui-pro/react/sidebar";
import { ChatConversation } from "@heroui-pro/react/chat-conversation";
import { ChatMessage } from "@heroui-pro/react/chat-message";
import { ChainOfThought } from "@heroui-pro/react/chain-of-thought";
import { ChatTool } from "@heroui-pro/react/chat-tool";
import { PromptInput } from "@heroui-pro/react/prompt-input";
import { ChatAttachment } from "@heroui-pro/react/chat-attachment";
import { DropZone } from "@heroui-pro/react/drop-zone";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import assistantAvatarUrl from "./assets/knowledge-helper-avatar.svg";
import brandLogoUrl from "./assets/knowledge-helper-logo.svg";
import userAvatarUrl from "./assets/user-avatar.svg";
import "./App.css";

type View = "chat" | "projects" | "knowledge" | "settings";
type TraceEvent = {
  kind:
    | "reasoning_summary"
    | "mcp_call"
    | "mcp_result"
    | "context"
    | "repair"
    | "final";
  title: string;
  detail: string;
};
type RetrievalDiagnostic = {
  query?: string;
  query_terms?: string[];
  candidate_chunks?: number;
  minimum_score?: number;
  result_count?: number;
  reason?: string;
  results?: Source[];
};
type AgentState = {
  compacted?: boolean;
  retrieval_repaired?: boolean;
  trace?: TraceEvent[];
  retrieval?: { query?: string; diagnostic?: RetrievalDiagnostic };
};
type ContextUsage = {
  used_tokens: number;
  threshold_tokens: number;
  max_tokens?: number;
  updated_at?: number | null;
};
type Source = {
  asset_id: string;
  name: string;
  page?: number;
  chunk_no: number;
  score?: number;
  excerpt?: string;
  preview_url?: string;
  download_url: string;
};
type Asset = {
  asset_id: string;
  project_id: string;
  name: string;
  kind: string;
  status: string;
  size_bytes: number;
  preview_url?: string;
  download_url: string;
  page_count?: number;
  chunk_count?: number;
  error?: string;
  tags: string[];
  description: string;
  vision_status: "unavailable" | "queued" | "processing" | "ready" | "empty" | "failed";
  version: {
    group_id: string;
    number: number;
    is_current: boolean;
    replaces_asset_id?: string;
  };
};
type UploadFeedback = {
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
type FilePreview = {
  assetId: string;
  name: string;
};
type Message = {
  id?: string;
  role: "user" | "assistant";
  content: string;
  created_at: number;
  sources?: Source[];
  attachments?: Asset[];
  knowledge_write?: { name?: string };
  agent?: AgentState;
  feedback?: {
    rating: "useful" | "not_useful";
    note?: string;
    updated_at?: number;
  };
};
type Conversation = {
  id: string;
  title: string;
  project_id: string;
  parent_id?: string;
  updated_at: number;
  message_count: number;
  has_context: boolean;
  compaction_count: number;
  context_usage: ContextUsage;
  pinned: boolean;
  messages: Message[];
};
type ConversationSummary = Omit<Conversation, "messages">;
type Project = {
  id: string;
  name: string;
  icon: string;
  color: string;
  instructions: string;
  memory_mode: string;
  asset_count: number;
  conversation_count: number;
};
type Pipeline = {
  chunk_size: number;
  chunk_overlap: number;
  boundary_mode: "natural" | "fixed";
  pdf_chunk_scope: "page" | "document";
  image_index_mode: "attachment_only" | "skip";
  top_k: number;
  minimum_score: number;
  embedding_adapter: "unconfigured" | "local_openai_compatible";
  embedding_model: string;
  embedding_base_url: string;
  vector_weight: number;
  reranker_adapter: "unconfigured" | "local_openai_compatible";
  reranker_model: string;
  reranker_base_url: string;
  rerank_top_n: number;
  vision_adapter: "unconfigured" | "local_openai_compatible";
  vision_model: string;
  vision_base_url: string;
  vision_max_pages: number;
};
type Runtime = {
  deepseek_model: string;
  deepseek_base_url: string;
  agent_max_turns: number;
  agent_context_compaction_tokens: number;
  storage_mode: string;
  api_key_configured: boolean;
};
type Status = {
  assets: number;
  documents: number;
  chunks: number;
  processing: number;
  model: string;
  agent_sdk_ready: boolean;
  pipeline: Pipeline;
  runtime: Runtime;
};
type EvaluationCase = {
  id: string;
  project_id: string;
  question: string;
  expected_answer: string;
  expected_sources: string[];
  created_at: number;
  updated_at: number;
};
type EvaluationResult = {
  case_id: string;
  question: string;
  expected_answer: string;
  expected_sources: string[];
  answer?: string;
  sources?: Array<{ name?: string }>;
  source_match?: boolean | null;
  error?: string;
  latency_ms?: number;
};
type EvaluationJob = {
  id: string;
  project_id: string;
  case_ids: string[];
  status: "queued" | "running" | "completed";
  total: number;
  completed: number;
  failed: number;
  results: EvaluationResult[];
  created_at: number;
};
type QualitySnapshot = { cases: EvaluationCase[]; jobs: EvaluationJob[] };

const DEFAULT_PROJECT = "local-default";
const conversationKey = "knowledge-helper-active-conversation-v2";
const projectKey = "knowledge-helper-active-project-v2";
const attachmentAccept = ".txt,.md,.markdown,.pdf,.png,.jpg,.jpeg,.webp";
const LONG_PASTE_THRESHOLD = 8_000;
const fallbackPipeline: Pipeline = {
  chunk_size: 900,
  chunk_overlap: 120,
  boundary_mode: "natural",
  pdf_chunk_scope: "page",
  image_index_mode: "attachment_only",
  top_k: 5,
  minimum_score: 0,
  embedding_adapter: "unconfigured",
  embedding_model: "",
  embedding_base_url: "",
  vector_weight: 0.3,
  reranker_adapter: "unconfigured",
  reranker_model: "",
  reranker_base_url: "",
  rerank_top_n: 20,
  vision_adapter: "unconfigured",
  vision_model: "",
  vision_base_url: "",
  vision_max_pages: 4,
};

const API_BASE_KEY = "knowledge-helper-api-base";
const isTauri = typeof window !== "undefined" && "__TAURI__" in window;
const STREAM_RECOVERY_TIMEOUT_MS = 95_000;
const STREAM_RECOVERY_POLL_INTERVAL_MS = 1_000;

function getApiBase(): string {
  const stored = localStorage.getItem(API_BASE_KEY);
  if (stored) return stored;
  // Tauri 桌面端默认使用云端地址，网页端用相对路径
  if (isTauri) return "https://64.83.38.223:8443";
  return "";
}

function fileViewUrl(assetId: string): string {
  return `${getApiBase()}/api/assets/${assetId}/view`;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(getApiBase() + url, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string" ? data.detail : "请求失败。",
    );
  return data as T;
}

async function readServerEvents(
  response: Response,
  onEvent: (event: string, data: Record<string, unknown>) => void,
): Promise<void> {
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string" ? data.detail : "请求失败。",
    );
  }
  if (!response.body) throw new Error("浏览器不支持流式响应。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (packet: string) => {
    let event = "message";
    let dataText = "";
    for (const line of packet.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) dataText += line.slice(5).trim();
    }
    if (!dataText) return;
    try {
      const data = JSON.parse(dataText);
      if (data && typeof data === "object") onEvent(event, data);
    } catch {
      // 忽略不完整或非 JSON 的服务端事件，等待后续事件继续渲染。
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const packets = buffer.split(/\r?\n\r?\n/);
    buffer = packets.pop() || "";
    packets.forEach(dispatch);
    if (done) break;
  }
  if (buffer.trim()) dispatch(buffer);
}

function hasPersistedCompleteTurn(
  conversation: Conversation,
  initialMessageCount: number,
): boolean {
  if (conversation.messages.length < initialMessageCount + 2) return false;
  const lastMessage = conversation.messages.at(-1);
  if (
    !lastMessage ||
    lastMessage.role !== "assistant" ||
    !lastMessage.content.trim()
  ) {
    return false;
  }
  return true;
}

async function waitForPersistedCompleteTurn(
  conversationId: string,
  initialMessageCount: number,
): Promise<Conversation | null> {
  const deadline = Date.now() + STREAM_RECOVERY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const persisted = await request<Conversation>(
      `/api/conversations/${conversationId}`,
    );
    if (hasPersistedCompleteTurn(persisted, initialMessageCount)) {
      return persisted;
    }
    await new Promise<void>((resolve) => window.setTimeout(resolve, STREAM_RECOVERY_POLL_INTERVAL_MS));
  }
  return null;
}

const formatTime = (value?: number) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
function formatFileSize(sizeBytes: number): string {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) return "大小未知";
  const units = ["B", "KB", "MB", "GB"];
  let value = sizeBytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = unitIndex === 0 ? Math.round(value) : Number(value.toFixed(1));
  return `${rounded} ${units[unitIndex]}`;
}

const wait = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));

const scrollPageToBottom = (behavior: ScrollBehavior = "auto") => {
  const container = document.querySelector<HTMLElement>(".conversation-scroll");
  container?.scrollTo({ top: container.scrollHeight, behavior });
};
const traceLabel = (kind: TraceEvent["kind"]) =>
  ({
    reasoning_summary: "执行思路",
    mcp_call: "工具调用",
    mcp_result: "工具结果",
    context: "上下文",
    repair: "修复",
    final: "完成",
  })[kind];

async function copyVisibleMessage(content: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(content);
    return;
  }
  const field = document.createElement("textarea");
  field.value = content;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  document.execCommand("copy");
  field.remove();
}

function CopyMessageAction({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);
  const canCopy = Boolean(content.trim());
  async function copyMessage() {
    if (!canCopy) return;
    try {
      await copyVisibleMessage(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }
  return (
    <ChatMessage.Action
      aria-label={copied ? "已复制消息" : "复制消息"}
      tooltip={copied ? "已复制" : "复制"}
      isDisabled={!canCopy}
      onPress={() => void copyMessage()}
    >
      {copied ? <Check size={15} /> : <Copy size={15} />}
    </ChatMessage.Action>
  );
}

type SelectOption = { id: string; label: string };
type ConfirmationAction = {
  title: string;
  description: string;
  actionLabel: string;
  onConfirm: () => Promise<void>;
};

function HeroSelect({
  value,
  options,
  onChange,
  ariaLabel,
  className,
}: {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <Select
      className={className}
      selectedKey={value}
      onSelectionChange={(key) => {
        if (key) onChange(String(key));
      }}
    >
      <Select.Trigger aria-label={ariaLabel}>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {options.map((option) => (
            <ListBox.Item
              key={option.id}
              id={option.id}
              textValue={option.label}
            >
              {option.label}
              <ListBox.Item.Indicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  );
}

const formatTokens = (value: number) =>
  new Intl.NumberFormat("zh-CN").format(Math.max(0, Math.round(value)));
const displayConversationTitle = (title: string) =>
  title.replace(/\s*·\s*分支$/, "");

const precedingUserQuestion = (messages: Message[], index: number) => {
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const message = messages[cursor];
    if (message?.role === "user" && message.content.trim()) return message.content;
  }
  return "";
};

function ContextMeter({
  usage,
  fallbackThreshold,
}: {
  usage?: ContextUsage;
  fallbackThreshold: number;
}) {
  const threshold = Math.max(1, fallbackThreshold);
  const sdkThreshold = Math.max(0, usage?.threshold_tokens || 0);
  const used = Math.max(0, usage?.used_tokens || 0);
  const percentage = Math.min(100, (used / threshold) * 100);
  const nearLimit = percentage >= 80;
  const label = `${formatTokens(used)} / ${formatTokens(threshold)} tokens`;
  const sdkThresholdHint = sdkThreshold > threshold
    ? `；SDK 自动压缩兜底阈值：${formatTokens(sdkThreshold)} tokens。`
    : "";
  return (
    <div
      className="context-meter"
      title={`主动压缩阈值：${label}；达到后由 Agent SDK 压缩较早记录。${sdkThresholdHint}`}
    >
      <div className="context-meter-label">
        <span>上下文</span>
        <span>{label}</span>
      </div>
      <ProgressBar
        aria-label="会话上下文 token 使用量"
        value={percentage}
        minValue={0}
        maxValue={100}
        size="sm"
        color={nearLimit ? "warning" : "accent"}
      >
        <ProgressBar.Track>
          <ProgressBar.Fill />
        </ProgressBar.Track>
      </ProgressBar>
    </div>
  );
}

function ConversationActionsMenu({
  conversation,
  onRename,
  onTogglePin,
  onExport,
  onDelete,
}: {
  conversation: ConversationSummary;
  onRename: () => void;
  onTogglePin: () => void;
  onExport: () => void;
  onDelete: () => void;
}) {
  const title = displayConversationTitle(conversation.title);
  return (
    <Sidebar.MenuActions className="history-conversation-actions">
      <Dropdown>
        <Dropdown.Trigger
          className="history-conversation-menu-trigger"
          aria-label={`打开会话「${title}」的操作菜单`}
        >
          <Ellipsis size={16} />
        </Dropdown.Trigger>
        <Dropdown.Popover
          className="history-conversation-popover"
          placement="right"
        >
          <Dropdown.Menu aria-label={`会话「${title}」的操作`}>
            <Dropdown.Item id="rename" textValue="重命名" onAction={onRename}>
              <Pencil size={15} />
              <span>重命名</span>
            </Dropdown.Item>
            <Dropdown.Item
              id="pin"
              textValue={conversation.pinned ? "取消置顶" : "置顶"}
              onAction={onTogglePin}
            >
              {conversation.pinned ? <PinOff size={15} /> : <Pin size={15} />}
              <span>{conversation.pinned ? "取消置顶" : "置顶"}</span>
            </Dropdown.Item>
            <Dropdown.Item id="export" textValue="导出 Markdown" onAction={onExport}>
              <Download size={15} />
              <span>导出 Markdown</span>
            </Dropdown.Item>
            <Dropdown.Item id="delete" textValue="删除" onAction={onDelete}>
              <Trash2 size={15} />
              <span>删除</span>
            </Dropdown.Item>
          </Dropdown.Menu>
        </Dropdown.Popover>
      </Dropdown>
    </Sidebar.MenuActions>
  );
}

function ExecutionTrace({ trace }: { trace: TraceEvent[] }) {
  return (
    <ChainOfThought className="execution-trace" defaultExpanded={true}>
      <ChainOfThought.Trigger>
        执行过程 · {trace.length} 步
      </ChainOfThought.Trigger>
      <ChainOfThought.Content>
        <p className="trace-caption">
          可核对的 Agent SDK 步骤与本地工具调用；不展示模型私有思维链。
        </p>
        <ChainOfThought.Steps>
          {trace.map((event, index) => (
            <ChainOfThought.Step
              key={`${event.kind}-${index}`}
              label={traceLabel(event.kind)}
            >
              <strong>{event.title}</strong>
              <span>{event.detail}</span>
            </ChainOfThought.Step>
          ))}
        </ChainOfThought.Steps>
        {trace
          .filter(
            (event) => event.kind === "mcp_call" || event.kind === "mcp_result",
          )
          .map((event, index) => (
            <ChatTool
              key={`${event.title}-${index}`}
              className="trace-tool"
              state={
                event.kind === "mcp_call"
                  ? "input-available"
                  : "output-available"
              }
              toolName={event.title.replace("工具调用：", "")}
              input={{
                summary:
                  event.kind === "mcp_call"
                    ? event.detail
                    : "已收到工具结果",
              }}
              output={{
                summary:
                  event.kind === "mcp_result"
                    ? event.detail
                    : "等待工具返回",
              }}
            />
          ))}
      </ChainOfThought.Content>
    </ChainOfThought>
  );
}

const retrievalReason = (reason?: string) =>
  ({
    matched: "已命中可引用片段",
    no_query_terms: "问题中没有可用于本地检索的词项",
    no_ready_documents: "当前项目没有已完成处理的资料",
    below_minimum_score: "存在相关片段，但得分低于当前最低阈值",
    no_matching_chunks: "已检索资料，但没有匹配的片段",
  })[reason || ""] || "检索结果待分析";

function RetrievalDiagnosticView({ retrieval }: { retrieval?: AgentState["retrieval"] }) {
  const diagnostic = retrieval?.diagnostic;
  if (!diagnostic || !Object.keys(diagnostic).length) return null;
  return (
    <details className="retrieval-diagnostic">
      <summary>检索诊断 · {retrievalReason(diagnostic.reason)}</summary>
      <Card>
        <Card.Content>
          <dl>
            <div><dt>实际检索词</dt><dd>{retrieval?.query || diagnostic.query || "—"}</dd></div>
            <div><dt>候选片段</dt><dd>{diagnostic.candidate_chunks ?? 0}</dd></div>
            <div><dt>命中片段</dt><dd>{diagnostic.result_count ?? 0}</dd></div>
            <div><dt>最低得分</dt><dd>{diagnostic.minimum_score ?? 0}</dd></div>
          </dl>
          <p className="retrieval-diagnostic-reason">{retrievalReason(diagnostic.reason)}</p>
        </Card.Content>
      </Card>
    </details>
  );
}

function MessageView({
  message,
  onFeedback,
  onRetry,
  onPreview,
}: {
  message: Message;
  onFeedback?: (messageId: string, rating: "useful" | "not_useful") => void;
  onRetry?: () => void;
  onPreview?: (file: FilePreview) => void;
}) {
  const user = message.role === "user";
  const visibleContent = message.content;
  const [expandedSourceIds, setExpandedSourceIds] = useState<Set<string>>(
    () => new Set(),
  );
  const sourcesByAsset = new Map<string, Source[]>();
  for (const source of message.sources || []) {
    sourcesByAsset.set(source.asset_id, [
      ...(sourcesByAsset.get(source.asset_id) || []),
      source,
    ]);
  }
  const displaySources = Array.from(sourcesByAsset.values()).map(
    ([source]) => source,
  );
  const toggleSource = (assetId: string) => {
    setExpandedSourceIds((previous) => {
      const next = new Set(previous);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  };
  const body = (
    <>
      <div className="message-meta">
        <span>{user ? "你" : "知识库助手"}</span>
        <time>{formatTime(message.created_at)}</time>
      </div>
      <ChatMessage.Bubble>
        <ChatMessage.Content>
          {message.content ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {visibleContent}
            </ReactMarkdown>
          ) : (
            <p className="streaming-placeholder">正在检索当前项目资料…</p>
          )}
        </ChatMessage.Content>
      </ChatMessage.Bubble>
      <ChatMessage.Actions className="message-actions">
        <CopyMessageAction content={visibleContent} />
        {!user && message.id ? (
          <>
            <ChatMessage.Action
              aria-label="这条回答有用"
              tooltip={message.feedback?.rating === "useful" ? "已标记有用" : "有用"}
              onPress={() => onFeedback?.(message.id!, "useful")}
            >
              {message.feedback?.rating === "useful" ? <Check size={15} /> : <ThumbsUp size={15} />}
            </ChatMessage.Action>
            <ChatMessage.Action
              aria-label="这条回答无用"
              tooltip={message.feedback?.rating === "not_useful" ? "已标记无用" : "无用"}
              onPress={() => onFeedback?.(message.id!, "not_useful")}
            >
              <ThumbsDown size={15} />
            </ChatMessage.Action>
            <ChatMessage.Action
              aria-label="使用原问题重新检索"
              tooltip="重新检索"
              onPress={onRetry}
            >
              <RefreshCw size={15} />
            </ChatMessage.Action>
          </>
        ) : null}
      </ChatMessage.Actions>
      {!user && message.agent?.trace?.length ? (
        <ExecutionTrace trace={message.agent.trace} />
      ) : null}
      {!user && displaySources.length ? (
        <div className="message-sources" aria-label="来源文件">
          {displaySources.map((source) => {
            const isSourceExpanded = expandedSourceIds.has(source.asset_id);
            const excerpts = sourcesByAsset.get(source.asset_id) || [];
            return (
              <div
                className={`compact-source-group${isSourceExpanded ? " is-expanded" : ""}`}
                key={source.asset_id}
              >
                <div className="compact-source">
                  <button
                    className="compact-source-name"
                    type="button"
                    title={source.name}
                    aria-expanded={isSourceExpanded}
                    aria-label={`${isSourceExpanded ? "收起" : "展开"}来源摘录 ${source.name}`}
                    onClick={() => toggleSource(source.asset_id)}
                  >
                    {source.name}
                  </button>
                  <button
                    className="compact-source-action"
                    type="button"
                    aria-label={`预览文件 ${source.name}`}
                    title="在线预览"
                    onClick={() =>
                      onPreview?.({ assetId: source.asset_id, name: source.name })
                    }
                  >
                    <Eye size={14} />
                  </button>
                  <a
                    className="compact-source-action"
                    href={source.download_url}
                    download
                    aria-label={`下载文件 ${source.name}`}
                    title="下载文件"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <Download size={14} />
                  </a>
                </div>
                {isSourceExpanded ? (
                  <div className="source-preview-card">
                    {excerpts.map((excerpt) => (
                      <article
                        className="source-preview-excerpt"
                        key={`${excerpt.asset_id}-${excerpt.page || "chunk"}-${excerpt.chunk_no}`}
                      >
                        <span>
                          {excerpt.page ? `第 ${excerpt.page} 页` : `片段 ${excerpt.chunk_no + 1}`}
                        </span>
                        <p>{excerpt.excerpt || "该片段没有可显示的文字摘录。"}</p>
                      </article>
                    ))}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
      {!user ? <RetrievalDiagnosticView retrieval={message.agent?.retrieval} /> : null}
      {user && message.attachments?.length ? (
        <div className="attachments">
          {message.attachments.map((asset) => (
            <button
              className="attachment-link"
              key={asset.asset_id}
              type="button"
              aria-label={`打开附件 ${asset.name}`}
              onClick={() => onPreview?.({ assetId: asset.asset_id, name: asset.name })}
            >
              <ChatAttachment
                className="attachment-card"
                mediaType={asset.kind === "image" ? "image" : "document"}
                name={asset.name}
                src={asset.preview_url}
              >
                <ChatAttachment.Preview />
                <ChatAttachment.Name
                  className="attachment-visible-name"
                  title={asset.name}
                >
                  {asset.name}
                </ChatAttachment.Name>
              </ChatAttachment>
            </button>
          ))}
        </div>
      ) : null}
      {!user && message.knowledge_write?.name ? (
        <p className="message-note">
          已写入本项目知识库：{message.knowledge_write.name}
        </p>
      ) : null}
      {!user && message.feedback?.note ? <p className="message-feedback-note">反馈：{message.feedback.note}</p> : null}
    </>
  );
  return user ? (
    <ChatMessage.User className="chat-message user-message">
      <ChatMessage.Avatar
        className="user-avatar"
        alt="你的头像"
        fallback="你"
        src={userAvatarUrl}
      />
      <ChatMessage.Body>{body}</ChatMessage.Body>
    </ChatMessage.User>
  ) : (
    <ChatMessage.Assistant className="chat-message">
      <ChatMessage.Avatar
        className="assistant-avatar"
        alt="知识库助手头像"
        fallback="KH"
        src={assistantAvatarUrl}
      />
      <ChatMessage.Body>{body}</ChatMessage.Body>
    </ChatMessage.Assistant>
  );
}

function UploadFeedbackList({ items }: { items: UploadFeedback[] }) {
  if (!items.length) return null;
  return (
    <div className="upload-feedback-list" aria-live="polite">
      {items.map((item) => (
        <div
          key={item.id}
          className={`upload-feedback-item upload-feedback-${item.status}${item.exiting ? " is-exiting" : ""}`}
        >
          <div className="upload-feedback-copy">
            <strong>{item.name}</strong>
            <span>{formatFileSize(item.sizeBytes)}</span>
          </div>
          <div className="upload-feedback-status">
            <span>{item.status === "available" ? "可用" : item.status === "failed" ? "失败" : "上传中"}</span>
            <span>{Math.round(item.progress)}%</span>
          </div>
          <div
            className="upload-feedback-progress"
            role="progressbar"
            aria-label={`${item.name} 上传进度`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(item.progress)}
          >
            <span style={{ transform: `scaleX(${item.progress / 100})` }} />
          </div>
          {item.error ? <p className="error-text">{item.error}</p> : null}
        </div>
      ))}
    </div>
  );
}

function AssetCard({
  asset,
  isNew,
  projects,
  onMove,
  onSaveMetadata,
  onReprocess,
  onReplace,
  onRestore,
  onPreview,
  onDelete,
}: {
  asset: Asset;
  isNew: boolean;
  projects: Project[];
  onMove: (projectId: string) => void;
  onSaveMetadata: (changes: { name: string; tags: string[]; description: string }) => Promise<void>;
  onReprocess: () => Promise<void>;
  onReplace: (file: File) => Promise<void>;
  onRestore: () => Promise<void>;
  onPreview: () => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [nameDraft, setNameDraft] = useState(asset.name);
  const [tagsDraft, setTagsDraft] = useState(asset.tags.join(", "));
  const [descriptionDraft, setDescriptionDraft] = useState(asset.description);
  const replacementInputRef = useRef<HTMLInputElement>(null);
  const visionLabel = {
    unavailable: "视觉未配置",
    queued: "等待视觉处理",
    processing: "视觉处理中",
    ready: "视觉已提取",
    empty: "未提取到可索引内容",
    failed: "视觉提取失败",
  }[asset.vision_status];

  useEffect(() => {
    setNameDraft(asset.name);
    setTagsDraft(asset.tags.join(", "));
    setDescriptionDraft(asset.description);
  }, [asset.description, asset.name, asset.tags]);

  async function saveMetadata() {
    await onSaveMetadata({
      name: nameDraft.trim() || asset.name,
      tags: tagsDraft.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
      description: descriptionDraft,
    });
    setEditing(false);
  }

  async function chooseReplacement(event: ChangeEvent<HTMLInputElement>) {
    const replacement = event.target.files?.[0];
    event.target.value = "";
    if (replacement) await onReplace(replacement);
  }

  return (
    <Card className={`asset-card${isNew ? " is-new" : ""}`}>
      <Card.Header>
        <div className="asset-card-title">
          <Card.Title>{asset.name}</Card.Title>
          <span>版本 v{asset.version.number}</span>
        </div>
        <Chip size="sm" variant="secondary">
          {asset.version.is_current
            ? asset.status === "ready"
              ? "当前可问答"
              : asset.status
            : "历史版本"}
        </Chip>
      </Card.Header>
      <Card.Content>
        {asset.preview_url ? (
          <img src={asset.preview_url} alt={`${asset.name} 预览`} />
        ) : null}
        <p>
          {asset.kind.toUpperCase()} · {formatFileSize(asset.size_bytes)} ·{" "}
          {asset.page_count || 0} 页 ·{" "}
          {asset.chunk_count || 0} 个片段
        </p>
        <p className="asset-vision-status">{visionLabel}</p>
        {asset.tags.length ? (
          <div className="asset-tags" aria-label="资料标签">
            {asset.tags.map((tag) => (
              <Chip key={tag} size="sm" variant="secondary">{tag}</Chip>
            ))}
          </div>
        ) : null}
        {asset.description && !editing ? <p className="asset-description">{asset.description}</p> : null}
        {asset.error ? <p className="error-text">{asset.error}</p> : null}
        {editing ? (
          <div className="asset-metadata-form">
            <label>
              文件名称
              <Input
                aria-label="文件名称"
                value={nameDraft}
                onChange={(event) => setNameDraft(event.target.value)}
              />
            </label>
            <label>
              资料标签
              <Input
                aria-label="资料标签，使用逗号分隔"
                value={tagsDraft}
                placeholder="例如：财务, 制度"
                onChange={(event) => setTagsDraft(event.target.value)}
              />
            </label>
            <label>
              资料说明
              <TextArea
                aria-label="资料说明"
                value={descriptionDraft}
                onChange={(event) => setDescriptionDraft(event.target.value)}
              />
            </label>
            <div className="asset-action-row">
              <Button size="sm" variant="secondary" onPress={() => void saveMetadata()}>
                <Save size={14} /> 保存资料信息
              </Button>
              <Button size="sm" variant="ghost" onPress={() => setEditing(false)}>
                取消
              </Button>
            </div>
          </div>
        ) : null}
        <div className="inline-select">
          <span>移动到</span>
          <HeroSelect
            ariaLabel="移动文件到项目"
            value={asset.project_id}
            options={projects.map((project) => ({
              id: project.id,
              label: project.name,
            }))}
            onChange={onMove}
          />
        </div>
      </Card.Content>
      <Card.Footer>
        <Button size="sm" variant="ghost" aria-label={`预览文件 ${asset.name}`} onPress={onPreview}>
          <Eye size={14} /> 预览
        </Button>
        <a href={asset.download_url} target="_blank" rel="noreferrer">
          <Download size={14} /> 下载
        </a>
        <div className="asset-action-row">
          <span title="编辑资料信息">
            <Button size="sm" variant="ghost" aria-label="编辑资料信息" onPress={() => setEditing((value) => !value)}>
              <Pencil size={14} />
            </Button>
          </span>
          <span title="重新处理文件">
            <Button size="sm" variant="ghost" aria-label="重新处理文件" onPress={() => void onReprocess()}>
              <RefreshCw size={14} />
            </Button>
          </span>
          {asset.version.is_current ? (
            <>
              <input
                ref={replacementInputRef}
                className="visually-hidden"
                type="file"
                accept={attachmentAccept}
                onChange={(event) => void chooseReplacement(event)}
              />
              <span title="替换为新版本">
                <Button size="sm" variant="ghost" aria-label="替换为新版本" onPress={() => replacementInputRef.current?.click()}>
                  <Upload size={14} />
                </Button>
              </span>
            </>
          ) : (
            <span title="恢复此版本">
              <Button size="sm" variant="ghost" aria-label="恢复此版本" onPress={() => void onRestore()}>
                <RotateCcw size={14} />
              </Button>
            </span>
          )}
          <span title="删除文件">
            <Button size="sm" variant="ghost" aria-label="删除文件" onPress={onDelete}>
              <Trash2 size={14} />
            </Button>
          </span>
        </div>
      </Card.Footer>
    </Card>
  );
}

function App() {
  const [view, setView] = useState<View>("chat");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(
    localStorage.getItem(projectKey) || DEFAULT_PROJECT,
  );
  const [assets, setAssets] = useState<Asset[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [pipeline, setPipeline] = useState<Pipeline>(fallbackPipeline);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [quality, setQuality] = useState<QualitySnapshot>({ cases: [], jobs: [] });
  const [evaluationForm, setEvaluationForm] = useState({
    question: "",
    expectedAnswer: "",
    expectedSources: "",
  });
  const [selectedEvaluationCaseIds, setSelectedEvaluationCaseIds] = useState<string[]>([]);
  const [apiKeyDraft, setApiKeyDraft] = useState("");
  const [question, setQuestion] = useState("");
  const [pendingChatFiles, setPendingChatFiles] = useState<File[]>([]);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploadFeedback, setUploadFeedback] = useState<UploadFeedback[]>([]);
  const [recentlyAddedAssetIds, setRecentlyAddedAssetIds] = useState<Set<string>>(
    new Set(),
  );
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [renameValue, setRenameValue] = useState("");
  const [renameConversationId, setRenameConversationId] = useState<string | null>(
    null,
  );
  const [projectForm, setProjectForm] = useState({
    name: "",
    icon: "✦",
    color: "indigo",
    instructions: "",
  });
  const [confirmation, setConfirmation] = useState<ConfirmationAction | null>(
    null,
  );
  const [previewFile, setPreviewFile] = useState<FilePreview | null>(null);
  const confirmDialog = useOverlayState();
  const renameDialog = useOverlayState();
  const previewDialog = useOverlayState();
  const chatFileInputRef = useRef<HTMLInputElement>(null);
  const activeRequestRef = useRef<AbortController | null>(null);
  const projectIdRef = useRef(projectId);

  const activeProject =
    projects.find((project) => project.id === projectId) || projects[0];
  const latestMessage =
    conversation?.messages[conversation.messages.length - 1];
  const hasMessages = Boolean(conversation?.messages.length);

  useEffect(() => {
    projectIdRef.current = projectId;
  }, [projectId]);

  useEffect(() => {
    if (view !== "chat") return;
    const frame = window.requestAnimationFrame(() => scrollPageToBottom());
    return () => window.cancelAnimationFrame(frame);
  }, [view, conversation?.messages.length, latestMessage?.content]);

  useEffect(() => {
    const activeJob = quality.jobs.find(
      (job) => job.status === "queued" || job.status === "running",
    );
    if (!activeJob) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const refreshed = await request<EvaluationJob>(
          `/api/evaluations/jobs/${activeJob.id}`,
        );
        if (cancelled) return;
        setQuality((current) => ({
          ...current,
          jobs: current.jobs.map((job) =>
            job.id === refreshed.id ? refreshed : job,
          ),
        }));
      } catch {
        // 轮询失败不打断资料库操作；下一次刷新会重新读取任务状态。
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [quality.jobs]);

  async function refresh(project = projectId, query = search) {
    const [projectData, assetData, conversationData, statusData, runtimeData, qualityData] =
      await Promise.all([
        request<{ projects: Project[] }>("/api/projects"),
        request<{ assets: Asset[] }>(
          `/api/assets?project_id=${encodeURIComponent(project)}`,
        ),
        request<{ conversations: ConversationSummary[] }>(
          `/api/conversations?project_id=${encodeURIComponent(project)}&q=${encodeURIComponent(query)}`,
        ),
        request<Status>(
          `/api/status?project_id=${encodeURIComponent(project)}`,
        ),
        request<Runtime>("/api/workspace-settings"),
        request<QualitySnapshot>(
          `/api/evaluations?project_id=${encodeURIComponent(project)}`,
        ),
      ]);
    setProjects(projectData.projects);
    setAssets(assetData.assets);
    setConversations(conversationData.conversations);
    setStatus(statusData);
    setPipeline(statusData.pipeline);
    setRuntime(runtimeData);
    setQuality(qualityData);
    return conversationData.conversations;
  }

  async function openConversation(id: string) {
    const next = await request<Conversation>(`/api/conversations/${id}`);
    setConversation(next);
    localStorage.setItem(conversationKey, id);
  }

  function openFilePreview(file: FilePreview) {
    setPreviewFile(file);
    previewDialog.open();
  }

  function newConversation() {
    if (busy) return;
    setView("chat");
    setSearch("");
    setConversation(null);
    localStorage.removeItem(conversationKey);
    setQuestion("");
    setPendingChatFiles([]);
    setError("");
    setNotice("");
  }

  useEffect(() => {
    void (async () => {
      try {
        const items = await refresh();
        const stored = localStorage.getItem(conversationKey);
        if (stored && items.some((item) => item.id === stored))
          await openConversation(stored);
        else if (items[0]) await openConversation(items[0].id);
        else {
          setConversation(null);
          localStorage.removeItem(conversationKey);
        }
      } catch (reason) {
        setError(
          reason instanceof Error ? reason.message : "本地服务连接失败。",
        );
      }
    })();
  }, []);

  async function changeProject(id: string) {
    setProjectId(id);
    localStorage.setItem(projectKey, id);
    setConversation(null);
    setSelectedEvaluationCaseIds([]);
    setEvaluationForm({ question: "", expectedAnswer: "", expectedSources: "" });
    try {
      const items = await refresh(id, "");
      if (items[0]) await openConversation(items[0].id);
      else newConversation();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "切换项目失败。");
    }
  }

  function queueChatFiles(files: FileList | File[]) {
    const selected = Array.from(files);
    if (!selected.length) return;
    setPendingChatFiles((current) => {
      const known = new Set(
        current.map((file) => `${file.name}:${file.size}:${file.lastModified}`),
      );
      return [
        ...current,
        ...selected.filter((file) => {
          const key = `${file.name}:${file.size}:${file.lastModified}`;
          if (known.has(key)) return false;
          known.add(key);
          return true;
        }),
      ];
    });
  }

  function removePendingChatFile(index: number) {
    setPendingChatFiles((current) =>
      current.filter((_, currentIndex) => currentIndex !== index),
    );
  }

  function handleQuestionPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const pastedText = event.clipboardData.getData("text/plain");
    const input = event.currentTarget;
    const selectionStart = input.selectionStart ?? input.value.length;
    const selectionEnd = input.selectionEnd ?? input.value.length;
    const nextLength =
      input.value.length - (selectionEnd - selectionStart) + pastedText.length;
    if (!pastedText || nextLength <= LONG_PASTE_THRESHOLD) return;

    event.preventDefault();
    const pastedFile = new File(
      [pastedText],
      `粘贴长文本-${Date.now()}.txt`,
      { type: "text/plain;charset=utf-8" },
    );
    queueChatFiles([pastedFile]);
    setNotice(
      `已将本次粘贴的 ${pastedText.length.toLocaleString("zh-CN")} 个字符转为 TXT 附件。`,
    );
  }

  function stopGeneration() {
    const activeRequest = activeRequestRef.current;
    if (!activeRequest || activeRequest.signal.aborted) return;
    activeRequest.abort();
    setNotice("已停止生成。");
  }

  async function waitForUploadedAssets(
    uploaded: Asset[],
    targetProjectId = projectId,
  ) {
    const ids = new Set(uploaded.map((asset) => asset.asset_id));
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      let data: { assets: Asset[] };
      try {
        data = await request<{ assets: Asset[] }>(
          `/api/assets?project_id=${encodeURIComponent(targetProjectId)}`,
        );
      } catch {
        await wait(500);
        continue;
      }
      const selected = data.assets.filter((asset) => ids.has(asset.asset_id));
      const failed = selected.find((asset) => asset.status === "failed");
      if (failed) {
        throw new Error(`${failed.name} 处理失败：${failed.error || "请检查文件内容。"}`);
      }
      if (
        selected.length === ids.size &&
        selected.every((asset) => asset.status === "ready")
      ) {
        return { assets: selected, allAssets: data.assets };
      }
      await wait(500);
    }
    throw new Error("文件仍在处理，未能确认已完成索引。请稍后在资料库中查看状态。");
  }

  async function sendQuestion() {
    const typedText = question.trim();
    const queuedFiles = pendingChatFiles;
    const text = typedText || (queuedFiles.length ? "请概述本轮上传的文件。" : "");
    if (!text || busy) return;
    setBusy(true);
    setError("");
    setNotice(
      queuedFiles.length
        ? "正在上传并解析本轮附件…"
        : "Agent SDK 正在检索当前项目的本地资料…",
    );
    let activeConversationId: string | undefined;
    let requestController: AbortController | null = null;
    try {
      let shouldRefreshConversationList = false;
      let current = conversation;
      const createdAt = Date.now();
      const pendingTrace: TraceEvent[] = [];
      const provisionalTitle =
        current?.title === "新建会话"
          ? text.length > 36
            ? `${text.slice(0, 36)}…`
            : text
          : current?.title || (text.length > 36 ? `${text.slice(0, 36)}…` : text);
      if (!current) {
        setConversation({
          id: `pending-${createdAt}`,
          title: provisionalTitle,
          project_id: projectId,
          updated_at: createdAt,
          message_count: 2,
          has_context: false,
          compaction_count: 0,
          context_usage: {
            used_tokens: 0,
            threshold_tokens: runtime?.agent_context_compaction_tokens || 60_000,
          },
          pinned: false,
          messages: [
            {
              role: "user",
              content: text,
              created_at: createdAt,
            },
            {
              role: "assistant",
              content: "",
              created_at: createdAt + 1,
              agent: { trace: pendingTrace },
            },
          ],
        });
        setQuestion("");
        window.requestAnimationFrame(() => scrollPageToBottom());
        const created = await request<Conversation>("/api/conversations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId }),
        });
        current = await request<Conversation>(`/api/conversations/${created.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: text }),
        });
        setSearch("");
        shouldRefreshConversationList = true;
      }
      activeConversationId = current.id;
      let uploadedAttachments: Asset[] = [];
      if (queuedFiles.length) {
        const uploaded = await uploadFilesToProject(queuedFiles);
        setNotice("附件已上传，正在本地解析并建立索引…");
        const ready = await waitForUploadedAssets(uploaded);
        uploadedAttachments = ready.assets;
        setAssets(ready.allAssets);
        setPendingChatFiles([]);
        setNotice("附件已就绪，Agent SDK 正在检索本轮资料…");
      }
      setConversation({
        ...current,
        title: provisionalTitle,
        updated_at: createdAt,
        messages: [
          ...current.messages,
          {
            role: "user",
            content: text,
            created_at: createdAt,
            attachments: uploadedAttachments,
          },
          {
            role: "assistant",
            content: "",
            created_at: createdAt + 1,
            agent: { trace: pendingTrace },
          },
        ],
      });
      window.requestAnimationFrame(() => scrollPageToBottom());
      localStorage.setItem(conversationKey, current.id);
      setQuestion("");
      requestController = new AbortController();
      activeRequestRef.current = requestController;
      if (shouldRefreshConversationList) {
        await refresh(projectId, "");
      }
      const response = await fetch(getApiBase() + "/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: requestController.signal,
        body: JSON.stringify({
          question: text,
          conversation_id: current.id,
          attachment_asset_ids: uploadedAttachments.map((asset) => asset.asset_id),
        }),
      });
      let completed = false;
      let streamedAnswer = "";
      const updatePendingAssistant = (changes: Partial<Message>) => {
        setConversation((previous) => {
          if (!previous || previous.id !== current.id) return previous;
          const messages = [...previous.messages];
          const lastIndex = messages.length - 1;
          const last = messages[lastIndex];
          if (!last || last.role !== "assistant") return previous;
          messages[lastIndex] = { ...last, ...changes };
          return { ...previous, messages };
        });
      };
      await readServerEvents(response, (event, data) => {
        if (event === "status" && typeof data.message === "string") {
          setNotice(data.message);
        }
        if (
          event === "trace" &&
          typeof data.kind === "string" &&
          typeof data.title === "string" &&
          typeof data.detail === "string"
        ) {
          pendingTrace.push({
            kind: data.kind as TraceEvent["kind"],
            title: data.title,
            detail: data.detail,
          });
          updatePendingAssistant({ agent: { trace: [...pendingTrace] } });
        }
        if (event === "delta" && typeof data.text === "string") {
          streamedAnswer += data.text;
          updatePendingAssistant({ content: streamedAnswer });
        }
        if (event === "done" && typeof data.conversation_id === "string") {
          completed = true;
          activeConversationId = data.conversation_id;
        }
        if (event === "error") {
          throw new Error(
            typeof data.message === "string" ? data.message : "问答失败。",
          );
        }
      });
      if (!completed || !activeConversationId) {
        setNotice("流连接中断，正在同步已保存的回答…");
        const persisted = await waitForPersistedCompleteTurn(
          current.id,
          current.messages.length,
        );
        if (!persisted) {
          throw new Error("流式回答未正常结束。");
        }
        completed = true;
        activeConversationId = persisted.id;
      }
      await refresh();
      await openConversation(activeConversationId);
      setNotice("");
    } catch (reason) {
      if (requestController?.signal.aborted) {
        setError("");
        setNotice("已停止生成。");
        return;
      }
      setError(reason instanceof Error ? reason.message : "问答失败。");
      setNotice("");
      if (activeConversationId) {
        try {
          await refresh();
          await openConversation(activeConversationId);
        } catch {
          // 保留首个错误提示；恢复历史失败不覆盖它。
        }
      }
    } finally {
      if (activeRequestRef.current === requestController) {
        activeRequestRef.current = null;
      }
      setBusy(false);
    }
  }

  async function uploadFilesToProject(files: FileList | File[]) {
    const uploaded: Asset[] = [];
    for (const file of Array.from(files)) {
      const body = new FormData();
      body.append("file", file);
      body.append("project_id", projectId);
      uploaded.push(await request<Asset>("/api/upload", { method: "POST", body }));
    }
    return uploaded;
  }

  async function uploadFiles(files: FileList | File[]) {
    const selectedFiles = Array.from(files);
    if (!selectedFiles.length || busy) return;
    const uploadProjectId = projectId;
    const records = selectedFiles.map((file, index) => ({
      id: `${Date.now()}-${index}-${file.name}`,
      projectId: uploadProjectId,
      name: file.name,
      sizeBytes: file.size,
      progress: 8,
      status: "uploading" as const,
    }));
    setUploadFeedback((current) => [...current, ...records]);
    setBusy(true);
    setError("");
    let failedCount = 0;

    for (const [index, file] of selectedFiles.entries()) {
      const record = records[index];
      const progressTimer = window.setInterval(() => {
        setUploadFeedback((current) =>
          current.map((item) =>
            item.id === record.id && item.status === "uploading"
              ? {
                  ...item,
                  progress: Math.min(item.assetId ? 92 : 68, item.progress + 4),
                }
              : item,
          ),
        );
      }, 180);

      try {
        const [uploadedAsset] = await uploadFilesToProject([file]);
        setUploadFeedback((current) =>
          current.map((item) =>
            item.id === record.id
              ? { ...item, assetId: uploadedAsset.asset_id, progress: Math.max(72, item.progress) }
              : item,
          ),
        );
        const ready = await waitForUploadedAssets([uploadedAsset], uploadProjectId);
        window.clearInterval(progressTimer);
        setUploadFeedback((current) =>
          current.map((item) =>
            item.id === record.id
              ? { ...item, status: "available", progress: 100 }
              : item,
          ),
        );
        if (projectIdRef.current === uploadProjectId) {
          setAssets(ready.allAssets);
          setRecentlyAddedAssetIds((current) =>
            new Set([...current, uploadedAsset.asset_id]),
          );
        }
        await wait(180);
        setUploadFeedback((current) =>
          current.map((item) =>
            item.id === record.id ? { ...item, exiting: true } : item,
          ),
        );
        await wait(180);
        setUploadFeedback((current) => current.filter((item) => item.id !== record.id));
        window.setTimeout(() => {
          setRecentlyAddedAssetIds((current) => {
            const next = new Set(current);
            next.delete(uploadedAsset.asset_id);
            return next;
          });
        }, 180);
      } catch (reason) {
        window.clearInterval(progressTimer);
        failedCount += 1;
        const message = reason instanceof Error ? reason.message : "上传失败。";
        setUploadFeedback((current) =>
          current.map((item) =>
            item.id === record.id
              ? { ...item, status: "failed", error: message }
              : item,
          ),
        );
      }
    }

    if (projectIdRef.current === uploadProjectId) {
      try {
        await refresh(uploadProjectId, search);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "资料列表刷新失败。");
      }
    }
    setNotice(
      failedCount
        ? `${selectedFiles.length - failedCount} 个文件可用，${failedCount} 个文件处理失败。`
        : "文件已写入存储并完成索引。",
    );
    if (failedCount) setError("部分文件未能完成上传或索引，请查看文件反馈。");
    setBusy(false);
  }

  async function saveAssetMetadata(
    asset: Asset,
    changes: { name: string; tags: string[]; description: string },
  ) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await request(`/api/assets/${asset.asset_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      });
      await refresh();
      setNotice(`已更新「${changes.name}」的本地资料信息。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "资料信息保存失败。");
    } finally {
      setBusy(false);
    }
  }

  async function reprocessAsset(asset: Asset) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await request(`/api/assets/${asset.asset_id}/reprocess`, { method: "POST" });
      await refresh();
      setNotice(`正在重新处理「${asset.name}」，完成后会更新本地索引。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重新处理失败。");
    } finally {
      setBusy(false);
    }
  }

  async function replaceAsset(asset: Asset, file: File) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      await request(`/api/assets/${asset.asset_id}/replace`, { method: "POST", body });
      await refresh();
      setNotice(`已上传「${file.name}」作为「${asset.name}」的新版本，正在本地处理。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "替换版本失败。");
    } finally {
      setBusy(false);
    }
  }

  async function restoreAssetVersion(asset: Asset) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await request(`/api/assets/${asset.asset_id}/restore`, { method: "POST" });
      await refresh();
      setNotice(`已恢复「${asset.name}」；当前检索会使用此版本。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "恢复版本失败。");
    } finally {
      setBusy(false);
    }
  }

  async function saveMessageFeedback(
    messageId: string,
    rating: "useful" | "not_useful",
  ) {
    if (!conversation) return;
    try {
      const feedback = await request<Message["feedback"]>(
        `/api/conversations/${conversation.id}/messages/${messageId}/feedback`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rating }),
        },
      );
      setConversation((current) => current ? {
        ...current,
        messages: current.messages.map((message) =>
          message.id === messageId ? { ...message, feedback } : message,
        ),
      } : current);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "反馈保存失败。");
    }
  }

  function retryQuestion(questionToRetry: string) {
    if (!questionToRetry.trim()) return;
    setQuestion(questionToRetry);
    setNotice("已填入原问题；请发送以重新检索当前项目资料。");
    window.requestAnimationFrame(() => {
      document
        .querySelector<HTMLTextAreaElement>(".composer-inline-textarea textarea, textarea.composer-inline-textarea")
        ?.focus();
    });
  }

  async function createEvaluationCase(event: FormEvent) {
    event.preventDefault();
    if (!evaluationForm.question.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      await request<EvaluationCase>("/api/evaluation-cases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: projectId,
          question: evaluationForm.question.trim(),
          expected_answer: evaluationForm.expectedAnswer.trim(),
          expected_sources: evaluationForm.expectedSources
            .split(/[,，]/)
            .map((name) => name.trim())
            .filter(Boolean),
        }),
      });
      setEvaluationForm({ question: "", expectedAnswer: "", expectedSources: "" });
      await refresh();
      setNotice("已添加本地评测题；选择后再开始评测。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建评测题失败。");
    } finally {
      setBusy(false);
    }
  }

  function toggleEvaluationCase(caseId: string) {
    setSelectedEvaluationCaseIds((current) =>
      current.includes(caseId)
        ? current.filter((item) => item !== caseId)
        : [...current, caseId],
    );
  }

  async function deleteEvaluationCase(caseItem: EvaluationCase) {
    if (busy) return;
    setBusy(true);
    try {
      await request(
        `/api/evaluation-cases/${caseItem.id}?project_id=${encodeURIComponent(projectId)}`,
        { method: "DELETE" },
      );
      setSelectedEvaluationCaseIds((current) => current.filter((item) => item !== caseItem.id));
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除评测题失败。");
    } finally {
      setBusy(false);
    }
  }

  async function startEvaluation() {
    if (!selectedEvaluationCaseIds.length || busy) return;
    setBusy(true);
    setError("");
    try {
      const job = await request<EvaluationJob>("/api/evaluations/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, case_ids: selectedEvaluationCaseIds }),
      });
      setQuality((current) => ({ ...current, jobs: [job, ...current.jobs] }));
      setNotice("本地评测已开始；每题会单独调用 Agent SDK，不使用聊天历史。 ");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "启动评测失败。");
    } finally {
      setBusy(false);
    }
  }

  async function patchConversation(id: string, changes: object) {
    const updated = await request<Conversation>(
      `/api/conversations/${id}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      },
    );
    await refresh();
    setConversation((current) => (current?.id === id ? updated : current));
    return updated;
  }
  function startRenameConversation(target: ConversationSummary) {
    setRenameValue(target.title);
    setRenameConversationId(target.id);
    renameDialog.open();
  }
  async function saveConversationTitle(event: FormEvent) {
    event.preventDefault();
    if (!renameValue.trim() || !renameConversationId) return;
    try {
      await patchConversation(renameConversationId, { title: renameValue.trim() });
      renameDialog.close();
      setRenameConversationId(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重命名失败。");
    }
  }
  function openConfirmation(action: ConfirmationAction) {
    setConfirmation(action);
    confirmDialog.open();
  }
  async function confirmAction() {
    if (!confirmation) return;
    try {
      await confirmation.onConfirm();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败。");
    } finally {
      confirmDialog.close();
      setConfirmation(null);
    }
  }
  function deleteConversation(target: ConversationSummary) {
    openConfirmation({
      title: "删除本地会话",
      description:
        "将删除此会话及其专属 SDK 会话转录。分支仍在使用的转录会被保留，直到最后一个分支删除。",
      actionLabel: "删除会话",
      onConfirm: async () => {
        const wasCurrent = conversation?.id === target.id;
        await request(`/api/conversations/${target.id}`, {
          method: "DELETE",
        });
        const items = await refresh();
        if (!wasCurrent) return;
        setConversation(null);
        localStorage.removeItem(conversationKey);
        if (items[0]) await openConversation(items[0].id);
        else await newConversation();
      },
    });
  }
  async function toggleConversationPin(target: ConversationSummary) {
    try {
      await patchConversation(target.id, { pinned: !target.pinned });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "置顶状态更新失败。");
    }
  }
  function exportConversation(id: string) {
    window.open(
      `/api/conversations/${id}/export?format=markdown`,
      "_blank",
      "noopener,noreferrer",
    );
  }
  async function rebuild() {
    setBusy(true);
    try {
      await request("/api/index/rebuild", { method: "POST" });
      await refresh();
      setNotice("已扫描目录并重建本地索引。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重建失败。");
    } finally {
      setBusy(false);
    }
  }
  async function savePipeline(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await request("/api/pipeline", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pipeline),
      });
      await refresh();
      setNotice("处理参数已保存，索引已重建。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "参数保存失败。");
    } finally {
      setBusy(false);
    }
  }
  async function saveRuntime(event: FormEvent) {
    event.preventDefault();
    if (!runtime) return;
    const hasNewApiKey = Boolean(apiKeyDraft.trim());
    setBusy(true);
    try {
      const next = await request<Runtime>("/api/workspace-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...runtime,
          deepseek_api_key: apiKeyDraft.trim() || undefined,
        }),
      });
      setRuntime(next);
      setApiKeyDraft("");
      setNotice(
        hasNewApiKey
          ? "运行参数已保存；DeepSeek API Key 已写入本机 .env。"
          : "Agent SDK 运行参数已保存。",
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "设置保存失败。");
    } finally {
      setBusy(false);
    }
  }
  async function saveProject(event: FormEvent) {
    event.preventDefault();
    try {
      const created = await request<Project>("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(projectForm),
      });
      setProjectForm({
        name: "",
        icon: "✦",
        color: "indigo",
        instructions: "",
      });
      await changeProject(created.id);
      setNotice("本地项目已创建，记忆和资料将独立隔离。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "项目创建失败。");
    }
  }
  async function updateProject() {
    if (!activeProject) return;
    try {
      await request(`/api/projects/${activeProject.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instructions: activeProject.instructions,
          name: activeProject.name,
          icon: activeProject.icon,
          color: activeProject.color,
        }),
      });
      await refresh();
      setNotice("项目指令已更新。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "项目更新失败。");
    }
  }
  async function deleteProject(project: Project) {
    openConfirmation({
      title: `删除项目「${project.name}」`,
      description: "项目必须没有会话和资料才可删除；默认项目不能删除。",
      actionLabel: "删除项目",
      onConfirm: async () => {
        await request(`/api/projects/${project.id}`, { method: "DELETE" });
        await changeProject(DEFAULT_PROJECT);
        setNotice("空项目已删除。");
      },
    });
  }

  return (
    <Sidebar.Provider
      className="app-shell"
      collapsible="offcanvas"
      variant="sidebar"
    >
      <Sidebar className="history-sidebar">
        <Sidebar.Header>
          <img className="brand-mark" src={brandLogoUrl} alt="知识库助手" />
          <div>
            <strong>Knowledge Helper</strong>
          </div>
        </Sidebar.Header>
        <Sidebar.Content>
          <Sidebar.Group>
            <Sidebar.Menu aria-label="工作区">
              {(
                [
                  ["chat", "新建对话"],
                  ["projects", "项目"],
                  ["knowledge", "知识库"],
                  ["settings", "设置"],
                ] as [View, string][]
              ).map(([id, label]) => (
                <Sidebar.MenuItem
                  key={id}
                  id={id}
                  isCurrent={view === id}
                  onAction={() => {
                    if (id === "chat") {
                      setView("chat");
                      void newConversation();
                      return;
                    }
                    setView(id);
                  }}
                  textValue={label}
                >
                  <Sidebar.MenuLabel>{label}</Sidebar.MenuLabel>
                </Sidebar.MenuItem>
              ))}
            </Sidebar.Menu>
          </Sidebar.Group>
          <Sidebar.Separator />
          <Sidebar.Group>
            <div className="sidebar-group-title">
              <Sidebar.GroupLabel>项目</Sidebar.GroupLabel>
              <Button
                size="sm"
                isIconOnly
                variant="ghost"
                aria-label="新建项目"
                onPress={() => setView("projects")}
              >
                <Plus size={16} />
              </Button>
            </div>
            <Sidebar.Menu aria-label="项目列表">
              {projects.map((project) => (
                <Sidebar.MenuItem
                  key={project.id}
                  id={project.id}
                  isCurrent={project.id === projectId}
                  onAction={() => void changeProject(project.id)}
                  textValue={project.name}
                >
                  <Sidebar.MenuItemContent>
                    <Sidebar.MenuLabel>
                      {project.icon} {project.name}
                    </Sidebar.MenuLabel>
                    <Sidebar.MenuChip>{project.asset_count}</Sidebar.MenuChip>
                  </Sidebar.MenuItemContent>
                </Sidebar.MenuItem>
              ))}
            </Sidebar.Menu>
          </Sidebar.Group>
          {view === "chat" ? (
            <>
              <Sidebar.Separator />
              <Sidebar.Group>
                <div className="sidebar-group-title">
                  <Sidebar.GroupLabel>会话历史</Sidebar.GroupLabel>
                  <Button
                    size="sm"
                    isIconOnly
                    variant="ghost"
                    aria-label="新建会话"
                    onPress={() => void newConversation()}
                  >
                    <Plus size={16} />
                  </Button>
                </div>
                <Input
                  className="sidebar-search"
                  value={search}
                  placeholder="搜索会话"
                  onChange={(event) => {
                    setSearch(event.target.value);
                    void refresh(projectId, event.target.value);
                  }}
                />
                <Sidebar.Menu aria-label="会话历史">
                  {conversations.map((item) => (
                    <Sidebar.MenuItem
                      key={item.id}
                      id={item.id}
                      isCurrent={item.id === conversation?.id}
                      onAction={() => void openConversation(item.id)}
                      textValue={displayConversationTitle(item.title)}
                    >
                      <Sidebar.MenuItemContent>
                        <div className="history-conversation-copy">
                          <Sidebar.MenuLabel>
                            {item.pinned ? "★ " : ""}
                            {displayConversationTitle(item.title)}
                          </Sidebar.MenuLabel>
                          <ContextMeter
                            usage={item.context_usage}
                            fallbackThreshold={
                              runtime?.agent_context_compaction_tokens || 60_000
                            }
                          />
                        </div>
                        <Sidebar.MenuChip>
                          {item.message_count}
                        </Sidebar.MenuChip>
                      </Sidebar.MenuItemContent>
                      <ConversationActionsMenu
                        conversation={item}
                        onRename={() => startRenameConversation(item)}
                        onTogglePin={() => void toggleConversationPin(item)}
                        onExport={() => exportConversation(item.id)}
                        onDelete={() => deleteConversation(item)}
                      />
                    </Sidebar.MenuItem>
                  ))}
                </Sidebar.Menu>
              </Sidebar.Group>
            </>
          ) : null}
        </Sidebar.Content>
      </Sidebar>
      <Sidebar.Main className="workspace-main">
        <header className="app-header">
          <div>
            <p className="eyebrow">LOCAL RAG · CLAUDE AGENT SDK</p>
            <h1>
              {activeProject
                ? `${activeProject.icon} ${activeProject.name}`
                : "本地知识库工作台"}
            </h1>
          </div>
        </header>
        <div className="workspace-content">
        {notice ? <p className="notice">{notice}</p> : null}
        {error ? <p className="notice error-text">{error}</p> : null}
        {view === "chat" ? (
          <section
            className={`chat-workspace ${hasMessages ? "has-messages" : "empty-chat-workspace"}`}
          >
            {hasMessages ? (
              <ChatConversation className="conversation-scroll">
                <ChatConversation.Content>
                  {conversation?.messages.map((message, index) => (
                    <MessageView
                      key={`${message.created_at}-${index}`}
                      message={message}
                      onPreview={openFilePreview}
                      onFeedback={(messageId, rating) =>
                        void saveMessageFeedback(messageId, rating)
                      }
                      onRetry={() =>
                        retryQuestion(
                          precedingUserQuestion(conversation?.messages || [], index),
                        )
                      }
                    />
                  ))}
                </ChatConversation.Content>
              </ChatConversation>
            ) : (
              <div className="empty-chat-landing">
                <h2>今天想了解什么？</h2>
              </div>
            )}
            <input
              ref={chatFileInputRef}
              className="chat-file-input"
              type="file"
              accept={attachmentAccept}
              multiple
              onChange={(event) => {
                queueChatFiles(event.target.files || []);
                event.target.value = "";
              }}
            />
            <div className="composer-dock">
              {pendingChatFiles.length ? (
                <div className="pending-chat-files" aria-label="本轮待发送附件">
                  {pendingChatFiles.map((file, index) => (
                    <ChatAttachment
                      className="pending-attachment-card"
                      key={`${file.name}-${file.size}-${file.lastModified}`}
                      mediaType="document"
                      name={file.name}
                    >
                      <ChatAttachment.Preview />
                      <ChatAttachment.Name
                        className="attachment-visible-name"
                        title={file.name}
                      >
                        {file.name}
                      </ChatAttachment.Name>
                      <ChatAttachment.Remove
                        aria-label={`移除附件 ${file.name}`}
                        onPress={() => removePendingChatFile(index)}
                      />
                    </ChatAttachment>
                  ))}
                </div>
              ) : null}
              <PromptInput
                value={question}
                layout="inline"
                maxHeight={240}
                onValueChange={setQuestion}
                onSubmit={() => void sendQuestion()}
                status={busy ? "streaming" : "ready"}
                onStop={stopGeneration}
                className="composer"
              >
                <PromptInput.Shell className="composer-inline-shell">
                  <PromptInput.Action
                    aria-label="添加本轮附件"
                    tooltip="添加文件"
                    isDisabled={busy}
                    onPress={() => chatFileInputRef.current?.click()}
                  >
                    <Paperclip size={17} />
                  </PromptInput.Action>
                  <PromptInput.TextArea
                    className="composer-inline-textarea"
                    placeholder="有问题，尽管问"
                    onPaste={handleQuestionPaste}
                  />
                  <PromptInput.Send
                    aria-label={busy ? "停止生成" : "发送问题"}
                    isDisabled={busy ? false : !question.trim() && !pendingChatFiles.length}
                  />
                </PromptInput.Shell>
              </PromptInput>
            </div>
          </section>
        ) : null}
        {view === "projects" ? (
          <section className="dashboard-grid">
            <Card>
              <Card.Header>
                <div>
                  <Card.Title>新建本地项目</Card.Title>
                  <Card.Description>
                    每个项目独立管理会话、资料和项目级指令。
                  </Card.Description>
                </div>
              </Card.Header>
              <Card.Content>
                <form className="stack-form" onSubmit={saveProject}>
                  <Input
                    required
                    placeholder="项目名称"
                    value={projectForm.name}
                    onChange={(event) =>
                      setProjectForm({
                        ...projectForm,
                        name: event.target.value,
                      })
                    }
                  />
                  <div className="form-row">
                    <Input
                      placeholder="图标"
                      value={projectForm.icon}
                      onChange={(event) =>
                        setProjectForm({
                          ...projectForm,
                          icon: event.target.value,
                        })
                      }
                    />
                    <HeroSelect
                      ariaLabel="选择项目颜色"
                      value={projectForm.color}
                      options={[
                        { id: "indigo", label: "靛蓝" },
                        { id: "emerald", label: "翡翠绿" },
                        { id: "amber", label: "琥珀" },
                        { id: "rose", label: "玫瑰" },
                      ]}
                      onChange={(color) =>
                        setProjectForm({ ...projectForm, color })
                      }
                    />
                  </div>
                  <TextArea
                    placeholder="项目级指令（仅本项目生效）"
                    value={projectForm.instructions}
                    onChange={(event) =>
                      setProjectForm({
                        ...projectForm,
                        instructions: event.target.value,
                      })
                    }
                  />
                  <Button type="submit">创建项目</Button>
                </form>
              </Card.Content>
            </Card>
            <Card>
              <Card.Header>
                <div>
                  <Card.Title>当前项目指令</Card.Title>
                  <Card.Description>
                    会话、文件和检索都不会跨项目引用。
                  </Card.Description>
                </div>
              </Card.Header>
              <Card.Content>
                {activeProject ? (
                  <div className="stack-form">
                    <Input
                      value={activeProject.name}
                      onChange={(event) =>
                        setProjects(
                          projects.map((item) =>
                            item.id === activeProject.id
                              ? { ...item, name: event.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                    <TextArea
                      value={activeProject.instructions}
                      onChange={(event) =>
                        setProjects(
                          projects.map((item) =>
                            item.id === activeProject.id
                              ? { ...item, instructions: event.target.value }
                              : item,
                          ),
                        )
                      }
                    />
                    <Button onPress={() => void updateProject()}>
                      保存项目指令
                    </Button>
                  </div>
                ) : null}
              </Card.Content>
            </Card>
            <div className="project-grid">
              {projects.map((project) => (
                <Card
                  key={project.id}
                  className={project.id === projectId ? "selected-card" : ""}
                >
                  <Card.Header>
                    <Card.Title>
                      {project.icon} {project.name}
                    </Card.Title>
                    <Chip size="sm" variant="secondary">
                      {project.memory_mode}
                    </Chip>
                  </Card.Header>
                  <Card.Content>
                    <p>{project.instructions || "未设置项目指令。"}</p>
                    <p>
                      {project.conversation_count} 个会话 ·{" "}
                      {project.asset_count} 份资料
                    </p>
                  </Card.Content>
                  <Card.Footer>
                    <Button
                      size="sm"
                      onPress={() => void changeProject(project.id)}
                    >
                      进入项目
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onPress={() => void deleteProject(project)}
                    >
                      删除空项目
                    </Button>
                  </Card.Footer>
                </Card>
              ))}
            </div>
          </section>
        ) : null}
        {view === "knowledge" ? (
          <section className="knowledge-workspace">
            <Card>
              <Card.Header>
                <div>
                  <Card.Title>项目资料库</Card.Title>
                  <Card.Description>
                    {status
                      ? `${status.assets} 份资料 · ${status.documents} 个可检索文档 · ${status.chunks} 个片段`
                      : "读取中…"}
                  </Card.Description>
                </div>
                <Button
                  variant="secondary"
                  isDisabled={busy}
                  onPress={() => void rebuild()}
                >
                  扫描并重建
                </Button>
              </Card.Header>
              <Card.Content>
                <DropZone>
                  <DropZone.Area>
                    <DropZone.Input
                      multiple
                      accept=".txt,.md,.markdown,.pdf,.png,.jpg,.jpeg,.webp"
                      onSelect={(files) => void uploadFiles(files)}
                    />
                    <DropZone.Icon>↑</DropZone.Icon>
                    <DropZone.Label>拖拽资料到此处</DropZone.Label>
                    <DropZone.Description>
                      支持 TXT、Markdown、PDF 与常见图片格式
                    </DropZone.Description>
                    <DropZone.Trigger aria-label="上传本地资料">
                      <Paperclip size={16} />
                    </DropZone.Trigger>
                  </DropZone.Area>
                </DropZone>
                <UploadFeedbackList
                  items={uploadFeedback.filter((item) => item.projectId === projectId)}
                />
              </Card.Content>
            </Card>
            <Card>
              <Card.Header>
                <div>
                  <Card.Title>处理与检索超参数</Card.Title>
                  <Card.Description>
                    所有配置立即保存到本机并重建索引。
                  </Card.Description>
                </div>
              </Card.Header>
              <Card.Content>
                <form className="pipeline-form" onSubmit={savePipeline}>
                  <label>
                    切片长度
                    <Input
                      type="number"
                      min="300"
                      max="3000"
                      value={pipeline.chunk_size}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          chunk_size: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <label>
                    重叠长度
                    <Input
                      type="number"
                      min="0"
                      max="1500"
                      value={pipeline.chunk_overlap}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          chunk_overlap: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <label>
                    切片边界
                    <HeroSelect
                      ariaLabel="选择切片边界"
                      value={pipeline.boundary_mode}
                      options={[
                        { id: "natural", label: "自然边界" },
                        { id: "fixed", label: "固定位置" },
                      ]}
                      onChange={(boundary_mode) =>
                        setPipeline({
                          ...pipeline,
                          boundary_mode:
                            boundary_mode as Pipeline["boundary_mode"],
                        })
                      }
                    />
                  </label>
                  <label>
                    PDF 范围
                    <HeroSelect
                      ariaLabel="选择 PDF 切片范围"
                      value={pipeline.pdf_chunk_scope}
                      options={[
                        { id: "page", label: "按页" },
                        { id: "document", label: "整篇" },
                      ]}
                      onChange={(pdf_chunk_scope) =>
                        setPipeline({
                          ...pipeline,
                          pdf_chunk_scope:
                            pdf_chunk_scope as Pipeline["pdf_chunk_scope"],
                        })
                      }
                    />
                  </label>
                  <label>
                    图片入库
                    <HeroSelect
                      ariaLabel="选择图片入库方式"
                      value={pipeline.image_index_mode}
                      options={[
                        { id: "attachment_only", label: "返回附件" },
                        { id: "skip", label: "跳过" },
                      ]}
                      onChange={(image_index_mode) =>
                        setPipeline({
                          ...pipeline,
                          image_index_mode:
                            image_index_mode as Pipeline["image_index_mode"],
                        })
                      }
                    />
                  </label>
                  <label>
                    视觉服务
                    <HeroSelect
                      ariaLabel="选择本机视觉服务"
                      value={pipeline.vision_adapter}
                      options={[
                        { id: "unconfigured", label: "仅作为附件" },
                        {
                          id: "local_openai_compatible",
                          label: "本机 OpenAI 兼容",
                        },
                      ]}
                      onChange={(vision_adapter) =>
                        setPipeline({
                          ...pipeline,
                          vision_adapter:
                            vision_adapter as Pipeline["vision_adapter"],
                        })
                      }
                    />
                  </label>
                  <label>
                    视觉模型
                    <Input
                      disabled={pipeline.vision_adapter === "unconfigured"}
                      value={pipeline.vision_model}
                      placeholder="例如：qwen2.5-vl"
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          vision_model: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    视觉地址
                    <Input
                      disabled={pipeline.vision_adapter === "unconfigured"}
                      value={pipeline.vision_base_url}
                      placeholder="http://127.0.0.1:9000/v1"
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          vision_base_url: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    视觉页数上限
                    <Input
                      type="number"
                      min="1"
                      max="12"
                      disabled={pipeline.vision_adapter === "unconfigured"}
                      value={pipeline.vision_max_pages}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          vision_max_pages: Number(event.target.value),
                        })
                      }
                    />
                    <span className="muted">仅允许本机 HTTP 地址；未配置时图片和扫描页仅作为附件返回。更新视觉配置后，请对需要识别的资料点击“重新处理”。</span>
                  </label>
                  <label>
                    Top-K
                    <Input
                      type="number"
                      min="1"
                      max="12"
                      value={pipeline.top_k}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          top_k: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <label>
                    最低得分
                    <Input
                      type="number"
                      min="0"
                      max="10"
                      step=".05"
                      value={pipeline.minimum_score}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          minimum_score: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <label>
                    嵌入服务
                    <HeroSelect
                      ariaLabel="选择嵌入服务"
                      value={pipeline.embedding_adapter}
                      options={[
                        { id: "unconfigured", label: "暂不配置" },
                        {
                          id: "local_openai_compatible",
                          label: "本机 OpenAI 兼容",
                        },
                      ]}
                      onChange={(embedding_adapter) =>
                        setPipeline({
                          ...pipeline,
                          embedding_adapter:
                            embedding_adapter as Pipeline["embedding_adapter"],
                        })
                      }
                    />
                  </label>
                  <label>
                    嵌入模型
                    <Input
                      disabled={pipeline.embedding_adapter === "unconfigured"}
                      value={pipeline.embedding_model}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          embedding_model: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    嵌入地址
                    <Input
                      disabled={pipeline.embedding_adapter === "unconfigured"}
                      value={pipeline.embedding_base_url}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          embedding_base_url: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    向量权重
                    <Input
                      type="number"
                      min="0"
                      max="1"
                      step=".05"
                      value={pipeline.vector_weight}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          vector_weight: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <label>
                    重排服务
                    <HeroSelect
                      ariaLabel="选择重排服务"
                      value={pipeline.reranker_adapter}
                      options={[
                        { id: "unconfigured", label: "暂不配置" },
                        {
                          id: "local_openai_compatible",
                          label: "本机 OpenAI 兼容",
                        },
                      ]}
                      onChange={(reranker_adapter) =>
                        setPipeline({
                          ...pipeline,
                          reranker_adapter:
                            reranker_adapter as Pipeline["reranker_adapter"],
                        })
                      }
                    />
                  </label>
                  <label>
                    重排模型
                    <Input
                      disabled={pipeline.reranker_adapter === "unconfigured"}
                      value={pipeline.reranker_model}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          reranker_model: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    重排地址
                    <Input
                      disabled={pipeline.reranker_adapter === "unconfigured"}
                      value={pipeline.reranker_base_url}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          reranker_base_url: event.target.value,
                        })
                      }
                    />
                  </label>
                  <label>
                    重排候选数
                    <Input
                      type="number"
                      min="1"
                      max="100"
                      value={pipeline.rerank_top_n}
                      onChange={(event) =>
                        setPipeline({
                          ...pipeline,
                          rerank_top_n: Number(event.target.value),
                        })
                      }
                    />
                  </label>
                  <div className="form-actions">
                    <Button type="submit" isDisabled={busy}>
                      保存并重建索引
                    </Button>
                  </div>
                </form>
              </Card.Content>
            </Card>
            <div className="assets-grid">
              {assets.map((asset) => (
                <AssetCard
                  key={asset.asset_id}
                  asset={asset}
                  isNew={recentlyAddedAssetIds.has(asset.asset_id)}
                  projects={projects}
                  onMove={(project) =>
                    void (async () => {
                      await request(`/api/assets/${asset.asset_id}`, {
                        method: "PATCH",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ project_id: project }),
                      });
                      await refresh();
                    })()
                  }
                  onSaveMetadata={(changes) => saveAssetMetadata(asset, changes)}
                  onReprocess={() => reprocessAsset(asset)}
                  onReplace={(file) => replaceAsset(asset, file)}
                  onRestore={() => restoreAssetVersion(asset)}
                  onPreview={() =>
                    openFilePreview({ assetId: asset.asset_id, name: asset.name })
                  }
                  onDelete={() =>
                    openConfirmation({
                      title: `删除文件「${asset.name}」`,
                      description:
                        "将移除该文件、其本地预览和对应索引片段；此操作不可恢复。",
                      actionLabel: "删除文件",
                      onConfirm: async () => {
                        await request(`/api/assets/${asset.asset_id}`, {
                          method: "DELETE",
                        });
                        await refresh();
                      },
                    })
                  }
                />
              ))}
            </div>
            <Card className="quality-card">
              <Card.Header>
                <div>
                  <Card.Title>评测题集</Card.Title>
                  <Card.Description>
                    创建后选择题目再开始评测。每题使用独立、无会话记忆的 Agent SDK 调用，结果只保存在本机质量记录中。
                  </Card.Description>
                </div>
                <Button
                  variant="secondary"
                  isDisabled={!selectedEvaluationCaseIds.length || busy}
                  onPress={() => void startEvaluation()}
                >
                  开始评测 {selectedEvaluationCaseIds.length ? `(${selectedEvaluationCaseIds.length})` : ""}
                </Button>
              </Card.Header>
              <Card.Content>
                <div className="quality-layout">
                  <form className="stack-form quality-case-form" onSubmit={createEvaluationCase}>
                    <label>
                      测试问题
                      <TextArea
                        required
                        aria-label="评测测试问题"
                        placeholder="例如：住宿报销上限是多少？"
                        value={evaluationForm.question}
                        onChange={(event) =>
                          setEvaluationForm({ ...evaluationForm, question: event.target.value })
                        }
                      />
                    </label>
                    <label>
                      预期要点（仅供人工核对）
                      <TextArea
                        aria-label="预期答案或验收要点"
                        placeholder="例如：应说明每晚五百元，并给出制度来源"
                        value={evaluationForm.expectedAnswer}
                        onChange={(event) =>
                          setEvaluationForm({ ...evaluationForm, expectedAnswer: event.target.value })
                        }
                      />
                    </label>
                    <label>
                      预期来源（可选，逗号分隔）
                      <Input
                        aria-label="预期来源"
                        placeholder="例如：差旅制度.md"
                        value={evaluationForm.expectedSources}
                        onChange={(event) =>
                          setEvaluationForm({ ...evaluationForm, expectedSources: event.target.value })
                        }
                      />
                    </label>
                    <Button type="submit" isDisabled={busy}>添加评测题</Button>
                  </form>
                  <div className="quality-case-list">
                    {quality.cases.length ? quality.cases.map((caseItem) => {
                      const selected = selectedEvaluationCaseIds.includes(caseItem.id);
                      return (
                        <Card key={caseItem.id} className="quality-case-item">
                          <Card.Header>
                            <Card.Title>{caseItem.question}</Card.Title>
                            <Chip size="sm" variant="secondary">
                              {selected ? "已选择" : "未选择"}
                            </Chip>
                          </Card.Header>
                          <Card.Content>
                            {caseItem.expected_answer ? <p>预期要点：{caseItem.expected_answer}</p> : null}
                            {caseItem.expected_sources.length ? <p>预期来源：{caseItem.expected_sources.join("、")}</p> : null}
                          </Card.Content>
                          <Card.Footer>
                            <Button size="sm" variant={selected ? "secondary" : "ghost"} onPress={() => toggleEvaluationCase(caseItem.id)}>
                              {selected ? "取消选择" : "选择题目"}
                            </Button>
                            <Button size="sm" variant="ghost" onPress={() => void deleteEvaluationCase(caseItem)}>
                              删除
                            </Button>
                          </Card.Footer>
                        </Card>
                      );
                    }) : <p className="muted">尚未创建评测题。题目与结果都仅保存在本机。</p>}
                  </div>
                </div>
                {quality.jobs[0] ? (
                  <div className="evaluation-job" aria-live="polite">
                    <div className="evaluation-job-heading">
                      <strong>最近评测</strong>
                      <Chip size="sm" variant="secondary">
                        {quality.jobs[0].status === "running" ? "进行中" : quality.jobs[0].status === "queued" ? "等待开始" : "已完成"}
                      </Chip>
                      <span>{quality.jobs[0].completed} / {quality.jobs[0].total} 题 · 失败 {quality.jobs[0].failed}</span>
                    </div>
                    <div className="evaluation-result-list">
                      {quality.jobs[0].results.map((result) => (
                        <Card key={result.case_id} className="evaluation-result">
                          <Card.Header><Card.Title>{result.question}</Card.Title></Card.Header>
                          <Card.Content>
                            {result.error ? <p className="error-text">{result.error}</p> : null}
                            {result.expected_answer ? <p>预期要点：{result.expected_answer}</p> : null}
                            {result.answer ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.answer}</ReactMarkdown> : null}
                            {result.expected_sources.length ? <p>来源核对：{result.source_match ? "已命中预期来源" : "未完全命中预期来源"}</p> : null}
                            {result.sources?.length ? <p>实际来源：{result.sources.map((source) => source.name || "未命名来源").join("、")}</p> : null}
                          </Card.Content>
                        </Card>
                      ))}
                    </div>
                  </div>
                ) : null}
              </Card.Content>
            </Card>
          </section>
        ) : null}
        {view === "settings" ? (
          <section className="settings-workspace">
            <Card>
                  <Card.Header>
                    <div>
                      <Card.Title>Agent SDK 运行设置</Card.Title>
                      <Card.Description>
                        可直接编辑本机 .env，或在此保存新的 DeepSeek API Key。
                      </Card.Description>
                </div>
              </Card.Header>
              <Card.Content>
                {runtime ? (
                  <form
                    className="stack-form settings-form"
                    onSubmit={saveRuntime}
                  >
                    <label>
                      DeepSeek 模型
                      <Input
                        value={runtime.deepseek_model}
                        onChange={(event) =>
                          setRuntime({
                            ...runtime,
                            deepseek_model: event.target.value,
                          })
                        }
                      />
                    </label>
                    <label>
                      Anthropic 兼容地址
                      <Input
                        value={runtime.deepseek_base_url}
                        onChange={(event) =>
                          setRuntime({
                            ...runtime,
                            deepseek_base_url: event.target.value,
                          })
                        }
                      />
                    </label>
                    <label>
                      DeepSeek API Key
                      <Input
                        type="password"
                        autoComplete="new-password"
                        value={apiKeyDraft}
                        placeholder="粘贴新的 DeepSeek API Key"
                        onChange={(event) => setApiKeyDraft(event.target.value)}
                      />
                      <span className="muted">
                        仅保存到本机 .env；已保存的 Key 不会显示，留空则不修改。
                      </span>
                    </label>
                    <label>
                      每轮最大步数
                      <Input
                        type="number"
                        min="1"
                        max="12"
                        value={runtime.agent_max_turns}
                        onChange={(event) =>
                          setRuntime({
                            ...runtime,
                            agent_max_turns: Number(event.target.value),
                          })
                        }
                      />
                    </label>
                    <label>
                      上下文压缩阈值（tokens）
                      <Input
                        type="number"
                        min="8000"
                        max="120000"
                        step="1000"
                        value={runtime.agent_context_compaction_tokens}
                        onChange={(event) =>
                          setRuntime({
                            ...runtime,
                            agent_context_compaction_tokens: Number(
                              event.target.value,
                            ),
                          })
                        }
                      />
                      <span className="muted">
                        每个会话独立计量；达到此阈值时由 Agent SDK
                        压缩较早上下文。
                      </span>
                    </label>
                    <p className="muted">
                      密钥状态：
                      {runtime.api_key_configured
                        ? "已从本机环境读取"
                        : "未配置"}{" "}
                      · 文件、项目和 SDK 会话均为本机保存。
                    </p>
                    <Button type="submit" isDisabled={busy}>
                      保存运行设置
                    </Button>
                  </form>
                ) : null}
              </Card.Content>
            </Card>
            <Card>
              <Card.Header>
                <div>
                  <Card.Title>本地数据边界</Card.Title>
                </div>
              </Card.Header>
              <Card.Content>
                <ul className="boundary-list">
                  <li>项目级记忆：只检索当前项目的已就绪资料。</li>
                  <li>
                    Agent SDK：无文件、Shell、网页或远程 MCP
                    工具，仅提供本地检索与受控写入。
                  </li>
                  <li>
                    写入知识库：必须同时满足“知识库助手启用写入”和“当前用户消息明确授权”。
                  </li>
                  <li>
                    删除会话或文件会显示本地确认弹窗，并只影响精确选择的本地记录。
                  </li>
                </ul>
              </Card.Content>
            </Card>
          </section>
        ) : null}
        </div>
            {renameConversationId ? (
          <Modal state={renameDialog}>
            <Modal.Backdrop>
              <Modal.Container size="sm">
                <Modal.Dialog>
                  <Modal.Header>
                    <Modal.Heading>重命名会话</Modal.Heading>
                    <Modal.CloseTrigger />
                  </Modal.Header>
                  <Modal.Body>
                    <form
                      id="rename-conversation-form"
                      onSubmit={saveConversationTitle}
                    >
                      <Input
                        autoFocus
                        aria-label="会话名称"
                        value={renameValue}
                        onChange={(event) => setRenameValue(event.target.value)}
                      />
                    </form>
                  </Modal.Body>
                  <Modal.Footer>
                        <Button
                          variant="ghost"
                          onPress={() => {
                            renameDialog.close();
                            setRenameConversationId(null);
                          }}
                    >
                      取消
                    </Button>
                    <Button type="submit" form="rename-conversation-form">
                      保存
                    </Button>
                  </Modal.Footer>
                </Modal.Dialog>
              </Modal.Container>
            </Modal.Backdrop>
          </Modal>
        ) : null}
        {confirmation ? (
          <Modal state={confirmDialog}>
            <Modal.Backdrop>
              <Modal.Container size="sm">
                <Modal.Dialog>
                  <Modal.Header>
                    <Modal.Heading>{confirmation.title}</Modal.Heading>
                    <Modal.CloseTrigger />
                  </Modal.Header>
                  <Modal.Body>
                    <p>{confirmation.description}</p>
                  </Modal.Body>
                  <Modal.Footer>
                    <Button
                      variant="ghost"
                      onPress={() => {
                        confirmDialog.close();
                        setConfirmation(null);
                      }}
                    >
                      取消
                    </Button>
                    <Button onPress={() => void confirmAction()}>
                      {confirmation.actionLabel}
                    </Button>
                  </Modal.Footer>
                </Modal.Dialog>
              </Modal.Container>
            </Modal.Backdrop>
          </Modal>
        ) : null}
            {previewFile ? (
              <Modal state={previewDialog}>
                <Modal.Backdrop>
                  <Modal.Container size="lg">
                    <Modal.Dialog>
                      <Modal.Header>
                        <Modal.Heading>{previewFile.name}</Modal.Heading>
                        <Modal.CloseTrigger />
                      </Modal.Header>
                      <Modal.Body>
                        <iframe
                          className="file-preview-frame"
                          title={`预览文件 ${previewFile.name}`}
                          src={fileViewUrl(previewFile.assetId)}
                        />
                      </Modal.Body>
                    </Modal.Dialog>
                  </Modal.Container>
                </Modal.Backdrop>
              </Modal>
            ) : null}
      </Sidebar.Main>
    </Sidebar.Provider>
  );
}

export default App;
