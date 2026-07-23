import { useEffect, useRef, useState } from "react";
import type { ClipboardEvent, FormEvent } from "react";
import {
  Check,
  Copy,
  Download,
  Ellipsis,
  Paperclip,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Trash2,
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
import { ChatSource } from "@heroui-pro/react/chat-source";
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
type AgentState = {
  compacted?: boolean;
  retrieval_repaired?: boolean;
  trace?: TraceEvent[];
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
  preview_url?: string;
  download_url: string;
};
type Asset = {
  asset_id: string;
  project_id: string;
  name: string;
  kind: string;
  status: string;
  preview_url?: string;
  download_url: string;
  page_count?: number;
  chunk_count?: number;
  error?: string;
};
type Message = {
  role: "user" | "assistant";
  content: string;
  created_at: number;
  sources?: Source[];
  attachments?: Asset[];
  knowledge_write?: { name?: string };
  agent?: AgentState;
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
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
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

const formatTime = (value?: number) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
const scrollPageToBottom = (behavior: ScrollBehavior = "auto") => {
  const bottom = Math.max(
    document.documentElement.scrollHeight,
    document.body.scrollHeight,
  );
  window.scrollTo({ top: bottom, behavior });
};
const redact = (content: string) =>
  content
    .replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, "sk-••••••••")
    .replace(
      /((?:(?:api[-_\s]?key|access[-_\s]?key|secret[-_\s]?key|personal[-_\s]?token|token|密码|password))\s*(?:是|为)?\s*[:：]\s*(?:\*{2}\s*)?`?)([^\s，,。；;`*]{4,})/gi,
      "$1已隐藏",
    )
    .replace(
      /((?:AK|SK)\s*(?:\([^\n)]{0,80}\))?[^:\n：]{0,16}[:：][\s*`]*)([A-Za-z0-9_-]{8,})/gi,
      "$1已隐藏",
    );
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
    <ChainOfThought className="execution-trace">
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
              <strong>{redact(event.title)}</strong>
              <span>{redact(event.detail)}</span>
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
              toolName={redact(
                event.title.replace("工具调用：", ""),
              )}
              input={{
                summary:
                  event.kind === "mcp_call"
                    ? redact(event.detail)
                    : "已收到工具结果",
              }}
              output={{
                summary:
                  event.kind === "mcp_result"
                    ? redact(event.detail)
                    : "等待工具返回",
              }}
            />
          ))}
      </ChainOfThought.Content>
    </ChainOfThought>
  );
}

function MessageView({ message }: { message: Message }) {
  const user = message.role === "user";
  const visibleContent = redact(message.content);
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
      </ChatMessage.Actions>
      {!user && message.agent?.trace?.length ? (
        <ExecutionTrace trace={message.agent.trace} />
      ) : null}
      {!user && message.sources?.length ? (
        <div className="message-sources">
          {message.sources.map((source) => (
            <ChatSource
              key={`${source.asset_id}-${source.chunk_no}`}
              href={source.preview_url || source.download_url}
              sourceType="document"
              title={`${source.name}${source.page ? ` · 第 ${source.page} 页` : ""}`}
            >
              <ChatSource.Trigger>
                <ChatSource.DocumentIcon />
                <ChatSource.Title>
                  {source.name}
                  {source.page ? ` · 第 ${source.page} 页` : ""}
                </ChatSource.Title>
              </ChatSource.Trigger>
            </ChatSource>
          ))}
        </div>
      ) : null}
      {message.attachments?.length ? (
        <div className="attachments">
          {message.attachments.map((asset) => (
            <a
              className="attachment-link"
              key={asset.asset_id}
              href={asset.download_url}
              target="_blank"
              rel="noreferrer"
              aria-label={`打开附件 ${asset.name}`}
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
            </a>
          ))}
        </div>
      ) : null}
      {!user && message.knowledge_write?.name ? (
        <p className="message-note">
          已写入本项目知识库：{message.knowledge_write.name}
        </p>
      ) : null}
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

function AssetCard({
  asset,
  projects,
  onMove,
  onDelete,
}: {
  asset: Asset;
  projects: Project[];
  onMove: (projectId: string) => void;
  onDelete: () => void;
}) {
  return (
    <Card className="asset-card">
      <Card.Header>
        <Card.Title>{asset.name}</Card.Title>
        <Chip size="sm" variant="secondary">
          {asset.status === "ready" ? "可问答" : asset.status}
        </Chip>
      </Card.Header>
      <Card.Content>
        {asset.preview_url ? (
          <img src={asset.preview_url} alt={`${asset.name} 预览`} />
        ) : null}
        <p>
          {asset.kind.toUpperCase()} · {asset.page_count || 0} 页 ·{" "}
          {asset.chunk_count || 0} 个片段
        </p>
        {asset.error ? <p className="error-text">{asset.error}</p> : null}
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
        <a href={asset.download_url} target="_blank" rel="noreferrer">
          下载
        </a>
        <Button size="sm" variant="ghost" onPress={onDelete}>
          删除
        </Button>
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
  const [apiKeyDraft, setApiKeyDraft] = useState("");
  const [question, setQuestion] = useState("");
  const [pendingChatFiles, setPendingChatFiles] = useState<File[]>([]);
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
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
  const confirmDialog = useOverlayState();
  const renameDialog = useOverlayState();
  const chatFileInputRef = useRef<HTMLInputElement>(null);
  const activeRequestRef = useRef<AbortController | null>(null);

  const activeProject =
    projects.find((project) => project.id === projectId) || projects[0];
  const latestMessage =
    conversation?.messages[conversation.messages.length - 1];
  const hasMessages = Boolean(conversation?.messages.length);

  useEffect(() => {
    if (view !== "chat") return;
    const frame = window.requestAnimationFrame(() => scrollPageToBottom());
    return () => window.cancelAnimationFrame(frame);
  }, [view, conversation?.messages.length, latestMessage?.content]);

  async function refresh(project = projectId, query = search) {
    const [projectData, assetData, conversationData, statusData, runtimeData] =
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
      ]);
    setProjects(projectData.projects);
    setAssets(assetData.assets);
    setConversations(conversationData.conversations);
    setStatus(statusData);
    setPipeline(statusData.pipeline);
    setRuntime(runtimeData);
    return conversationData.conversations;
  }

  async function openConversation(id: string) {
    const next = await request<Conversation>(`/api/conversations/${id}`);
    setConversation(next);
    localStorage.setItem(conversationKey, id);
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

  async function waitForUploadedAssets(uploaded: Asset[]) {
    const ids = new Set(uploaded.map((asset) => asset.asset_id));
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      const data = await request<{ assets: Asset[] }>(
        `/api/assets?project_id=${encodeURIComponent(projectId)}`,
      );
      const selected = data.assets.filter((asset) => ids.has(asset.asset_id));
      const failed = selected.find((asset) => asset.status === "failed");
      if (failed) {
        throw new Error(`${failed.name} 解析失败：${failed.error || "请检查文件内容。"}`);
      }
      if (selected.length === ids.size && selected.every((asset) => asset.status === "ready")) {
        return { assets: selected, allAssets: data.assets };
      }
      await new Promise<void>((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error("文件仍在解析中，请稍后重试。已上传文件会保留在本地资料库。");
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
      let current = conversation;
      if (!current) {
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
        await refresh(projectId, "");
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
      const createdAt = Date.now();
      const pendingTrace: TraceEvent[] = [];
      const provisionalTitle =
        current.title === "新建会话"
          ? text.length > 36
            ? `${text.slice(0, 36)}…`
            : text
          : current.title;
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
      const response = await fetch("/api/chat/stream", {
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
        throw new Error("流式回答未正常结束。");
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
    if (!files.length || busy) return;
    setBusy(true);
    setError("");
    try {
      await uploadFilesToProject(files);
      await refresh();
      setNotice("文件已上传，正在本地解析、切片并建立索引。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上传失败。");
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
      </Sidebar.Main>
    </Sidebar.Provider>
  );
}

export default App;
