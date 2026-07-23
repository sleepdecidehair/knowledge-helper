# Knowledge Helper Frontend / 知识库助手前端

The web client for Knowledge Helper is built with React, Vite, TypeScript, and
HeroUI. It renders local projects, conversations, document sources, upload
status, configurable ingestion settings, and the streaming Agent execution log.

知识库助手的 Web 客户端基于 React、Vite、TypeScript 与 HeroUI。它呈现本机
项目、会话、文档来源、上传状态、可配置的知识处理设置，以及流式 Agent 执行记录。

## Development / 开发

```bash
npm ci
npm run dev
```

The backend must be available at `http://127.0.0.1:8000` during development.
The Vite development server proxies `/api` and `/assets` to it.

开发时后端必须运行在 `http://127.0.0.1:8000`；Vite 开发服务器会代理 `/api`
和 `/assets` 请求。

## Verification / 验证

```bash
node --test tests/*.test.mjs
npm run lint
npm run build
```

Build output is written to `dist/` and intentionally excluded from Git. The
FastAPI application serves that build in a local production-style run.

构建产物位于 `dist/`，并被有意排除在 Git 之外；FastAPI 会在本机生产式运行中
直接提供该构建。
