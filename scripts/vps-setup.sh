#!/bin/bash
# === Knowledge Helper VPS 一键初始化脚本 ===
# 在 VPS 上以 root 运行: bash vps-setup.sh
# 将会: 创建部署用户、安装 Docker、生成 TLS 证书、克隆仓库

set -euo pipefail

DEPLOY_USER="deploy"
APP_DIR="/opt/knowledge-helper"
GIT_REPO="https://github.com/sleepdecidehair/knowledge-helper.git"
CERT_DIR="/opt/knowledge-helper/nginx/certs"

echo "=== 1. 创建部署用户 ==="
if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$DEPLOY_USER"
    mkdir -p /home/$DEPLOY_USER/.ssh
    # 请将你的 SSH 公钥放入此文件
    echo "# 粘贴你的 SSH 公钥到这里" > /home/$DEPLOY_USER/.ssh/authorized_keys
    chmod 700 /home/$DEPLOY_USER/.ssh
    chmod 600 /home/$DEPLOY_USER/.ssh/authorized_keys
    chown -R $DEPLOY_USER:$DEPLOY_USER /home/$DEPLOY_USER/.ssh
    echo "用户 $DEPLOY_USER 已创建。请编辑 /home/$DEPLOY_USER/.ssh/authorized_keys 添加你的 SSH 公钥。"
fi

echo ""
echo "=== 2. 安装 Docker ==="
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    usermod -aG docker "$DEPLOY_USER"
    echo "Docker 安装完成。"
else
    echo "Docker 已安装: $(docker --version)"
fi

echo ""
echo "=== 3. 创建应用目录 ==="
mkdir -p "$APP_DIR" "$CERT_DIR"
chown -R $DEPLOY_USER:$DEPLOY_USER /opt/knowledge-helper

echo ""
echo "=== 4. 生成自签名 TLS 证书 (有效期 10 年) ==="
if [ ! -f "$CERT_DIR/server.crt" ]; then
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$CERT_DIR/server.key" \
        -out "$CERT_DIR/server.crt" \
        -subj "/C=CN/ST=Zhejiang/L=Hangzhou/O=SelfSigned/CN=knowledge-helper"
    chmod 600 "$CERT_DIR/server.key"
    chmod 644 "$CERT_DIR/server.crt"
    echo "自签名证书已生成。"
else
    echo "证书已存在，跳过。"
fi

echo ""
echo "=== 5. 创建环境变量模板 ==="
cat > "$APP_DIR/.env.production" << 'ENVEOF'
# === 必填: DeepSeek API Key ===
DEEPSEEK_API_KEY=your-deepseek-api-key

# === DeepSeek 配置 ===
DEEPSEEK_ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_MODEL=deepseek-v4-flash

# === Agent 配置 ===
AGENT_TOOL_BASE_URL=http://backend:8000
AGENT_MAX_TURNS=6
AGENT_CONTEXT_COMPACTION_TOKENS=60000
AGENT_TIMEOUT_SECONDS=90

# === 检索配置 ===
TOP_K=5
CHUNK_SIZE=900
CHUNK_OVERLAP=120
MAX_UPLOAD_MB=15

# === S3 对象存储 (RainS3) ===
S3_ENDPOINT=https://cn-nb1.rains3.com
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_BUCKET=knowledge
ENVEOF

cat > "$APP_DIR/.env.test" << 'ENVEOF'
DEEPSEEK_API_KEY=your-deepseek-api-key
DEEPSEEK_ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_MODEL=deepseek-v4-flash
AGENT_TOOL_BASE_URL=http://backend:8000
AGENT_MAX_TURNS=6
AGENT_CONTEXT_COMPACTION_TOKENS=60000
AGENT_TIMEOUT_SECONDS=90
TOP_K=5
CHUNK_SIZE=900
CHUNK_OVERLAP=120
MAX_UPLOAD_MB=15
S3_ENDPOINT=https://cn-nb1.rains3.com
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_BUCKET=knowledge
ENVEOF

chown -R $DEPLOY_USER:$DEPLOY_USER /opt/knowledge-helper

echo ""
echo "=== 初始化完成 ==="
echo "下一步手动操作:"
echo "1. 编辑 /home/$DEPLOY_USER/.ssh/authorized_keys 添加你的 SSH 公钥"
echo "2. 编辑 $APP_DIR/.env.production 填写真实凭证"
echo "3. 编辑 $APP_DIR/.env.test 填写测试环境凭证"
echo "4. 在服务器上: su - $DEPLOY_USER"
echo "5. cd $APP_DIR && git clone $GIT_REPO ."
echo "6. 配置 GitHub Actions Secrets (见 README)"
echo ""
echo "⚠️  请勿使用 root 密码登录，使用 SSH 密钥认证!"
