from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"


def deploy_test_script() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  deploy-test:")
    end = workflow.index("  # ---------- 部署到生产环境", start)
    return workflow[start:end]


def test_test_deployment_targets_live_server_and_fails_fast():
    script = deploy_test_script()

    assert "set -euo pipefail" in script
    assert "DEPLOY_DIR=/opt/knowledge-helper-private" in script
    assert 'cd "$DEPLOY_DIR"' in script
    assert 'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15" \\' in script
    assert 'git fetch --depth=1 origin "$DEPLOY_SHA"' in script
    assert 'git checkout --detach --force "${DEPLOY_SHA}"' in script
    assert "docker compose -f docker-compose.yml -f docker-compose.test.yml -p kh-test up -d --wait" in script
    assert "curl -kfsS" in script
    assert "/api/status" in script


def test_test_deployment_cleans_only_old_project_images_after_health_check():
    script = deploy_test_script()

    health_check = script.index("curl -kfsS")
    cleanup = script.index("cleanup_repository")

    assert health_check < cleanup
    assert "ghcr.io/sleepdecidehair/knowledge-helper-backend" in script
    assert "ghcr.io/sleepdecidehair/knowledge-helper-frontend" in script
    assert "docker container prune" not in script
