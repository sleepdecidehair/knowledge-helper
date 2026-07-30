# 本地项目自助开库能力 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 本地项目可通过受限 MySQL 账号安全创建独立数据库和独立应用账号，不暴露 MySQL root 权限。

**Architecture:** 服务器 MySQL 提供一个 SQL SECURITY DEFINER 存储过程，由只拥有 EXECUTE 权限且仅允许本机公网 IP 连接的 db_provisioner 调用。仓库提供 Python CLI：生成应用密码、调用该过程、把新项目凭据写进 Git 忽略的本地文件；服务器安装脚本只通过 /root/.my.cnf 使用已保存的数据库 root 凭据。

**Tech Stack:** MySQL 5.7 存储过程、PyMySQL、Python 3.13、pytest、POSIX shell。

## Global Constraints

- 数据库名必须匹配 ^[a-z][a-z0-9_]{2,28}$，应用账号固定为 <database>_app。
- db_provisioner 只授予 EXECUTE，不可直接建库、建用户、授权或读取业务数据。
- 首次部署仅允许来源 58.38.101.149 连接 db_provisioner；不得使用 %。
- 应用账号权限仅覆盖其自身数据库，不含 GRANT OPTION 和全局权限。
- 密码不得进入 Git、测试输出、CI 日志或聊天消息。
- MySQL 5.7 DDL 不可回滚；存储过程只能清理本次调用新建的资源，绝不触碰既有资源。

---

## File Structure

- sql/db_provisioner.sql — 创建 db_control schema 和受限的 create_project_database 存储过程。
- scripts/install-db-provisioner.sh — 仅服务器 root 运行；安装 SQL、创建 IP 限制的 db_provisioner、保存其服务器端凭据。
- scripts/create_project_database.py — 本地 CLI；验证名称、生成应用密码、调用存储过程并保存项目凭据。
- tests/test_db_provisioner_sql.py — 校验 SQL 的权限边界、输入校验和清理路径。
- tests/test_create_project_database.py — CLI 纯函数与调用、凭据落盘行为的单元测试。
- .env.example — 列出不含密码的 provisioner 连接变量。
- .gitignore — 忽略 provisioner 与新项目凭据目录。
- docs/服务器MySQL自助建库操作手册.md — 用户操作手册和故障处理。

### Task 1: 本地开库 CLI 与单元测试

**Files:**
- Create: scripts/create_project_database.py
- Create: tests/test_create_project_database.py
- Modify: .env.example
- Modify: .gitignore

**Interfaces:**
- Consumes: MYSQL_PROVISIONER_HOST、MYSQL_PROVISIONER_PORT、MYSQL_PROVISIONER_USER、MYSQL_PROVISIONER_PASSWORD 环境变量。
- Produces: python scripts/create_project_database.py --project <name> --credentials-file <path>；成功时只输出数据库名、应用账号和凭据文件路径。
- Calls: CALL db_control.create_project_database(%s, %s, %s)。

- [ ] **Step 1: 写入失败测试**

~~~python
from scripts.create_project_database import app_user_name, validate_project_name

def test_validate_project_name_accepts_only_safe_mysql_identifiers():
    assert validate_project_name("nuo_car") == "nuo_car"
    for value in ("Nuocar", "ab", "a-b", "a;DROP", "a" * 29):
        try:
            validate_project_name(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe name accepted: {value}")

def test_app_user_name_is_deterministic_and_within_mysql_limit():
    assert app_user_name("nuo_car") == "nuo_car_app"
    assert len(app_user_name("a" * 28)) == 32
~~~

- [ ] **Step 2: 运行失败测试确认红灯**

Run: .venv/bin/python -m pytest tests/test_create_project_database.py -q

Expected: FAIL，提示 scripts.create_project_database 尚不存在。

- [ ] **Step 3: 实现最小 CLI**

实现下列稳定接口：

~~~python
PROJECT_NAME = re.compile(r"^[a-z][a-z0-9_]{2,28}$")

def validate_project_name(value: str) -> str:
    if not PROJECT_NAME.fullmatch(value):
        raise ValueError("项目名必须是 3-29 位小写字母、数字或下划线，并以字母开头")
    return value

def app_user_name(database: str) -> str:
    return f"{validate_project_name(database)}_app"

def generate_application_password() -> str:
    return secrets.token_urlsafe(32)

def provision_database(connection, database: str, password: str) -> tuple[str, str]:
    database = validate_project_name(database)
    user = app_user_name(database)
    with connection.cursor() as cursor:
        cursor.execute(
            "CALL db_control.create_project_database(%s, %s, %s)",
            (database, user, password),
        )
        result = cursor.fetchone()
    if result != (database, user):
        raise RuntimeError("数据库服务未返回预期的创建结果")
    return result

def write_credentials(path: Path, *, host: str, port: int, database: str,
                      user: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"MYSQL_HOST={host}\\nMYSQL_PORT={port}\\nMYSQL_DATABASE={database}\\n"
        f"MYSQL_USER={user}\\nMYSQL_PASSWORD={password}\\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)
~~~

generate_application_password 使用 secrets.token_urlsafe(32)。write_credentials 通过 os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) 创建新文件；已存在文件抛出 FileExistsError。CLI 使用 PyMySQL 参数化调用存储过程；调用失败不创建凭据文件；标准输出不得包含密码。

- [ ] **Step 4: 补齐凭据写入与参数化调用测试**

~~~python
class FakeCursor:
    def __init__(self):
        self.calls = []
    def execute(self, statement, values):
        self.calls.append((statement, values))
    def fetchone(self):
        return ("nuo_car", "nuo_car_app")
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False

class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
    def cursor(self):
        return self._cursor

def test_write_credentials_creates_owner_only_file(tmp_path):
    destination = tmp_path / "nuo_car.env"
    write_credentials(destination, host="db.example", port=3306,
                      database="nuo_car", user="nuo_car_app", password="secret")
    assert oct(destination.stat().st_mode & 0o777) == "0o600"
    assert "MYSQL_DATABASE=nuo_car" in destination.read_text()

def test_provision_database_calls_the_restricted_procedure():
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    assert provision_database(connection, "nuo_car", "password")[0] == "nuo_car"
    assert cursor.calls == [
        ("CALL db_control.create_project_database(%s, %s, %s)",
         ("nuo_car", "nuo_car_app", "password"))
    ]
~~~

- [ ] **Step 5: 更新忽略与示例配置**

在 .gitignore 加入：

~~~gitignore
.provisioning/
.project-databases/
~~~

在 .env.example 增加：

~~~dotenv
MYSQL_PROVISIONER_HOST=64.83.38.223
MYSQL_PROVISIONER_PORT=3306
MYSQL_PROVISIONER_USER=db_provisioner
MYSQL_PROVISIONER_PASSWORD=
~~~

- [ ] **Step 6: 运行测试并提交**

Run: .venv/bin/python -m pytest tests/test_create_project_database.py -q

Expected: PASS。

~~~bash
git add scripts/create_project_database.py tests/test_create_project_database.py .env.example .gitignore
git commit -m "feat: add local database provisioning client"
~~~

### Task 2: 受限 MySQL 存储过程与 SQL 边界测试

**Files:**
- Create: sql/db_provisioner.sql
- Create: tests/test_db_provisioner_sql.py

**Interfaces:**
- Consumes: p_database、p_application_user、p_application_password。
- Produces: db_control.create_project_database；成功返回数据库名和应用账号。
- Consumed by: Task 1 CLI 与 Task 3 安装脚本。

- [ ] **Step 1: 写入 SQL 契约失败测试**

~~~python
SQL = Path("sql/db_provisioner.sql").read_text(encoding="utf-8")

def test_procedure_restricts_names_and_creates_only_project_scoped_privileges():
    assert "REGEXP '^[a-z][a-z0-9_]{2,28}$'" in SQL
    assert "CONCAT(p_database, '_app')" in SQL
    assert "GRANT OPTION" not in SQL

def test_procedure_does_not_overwrite_existing_resources_and_cleans_its_own_failures():
    assert "information_schema.SCHEMATA" in SQL
    assert "mysql.user" in SQL
    assert "DECLARE EXIT HANDLER FOR SQLEXCEPTION" in SQL
    assert "DROP USER" in SQL
    assert "DROP DATABASE" in SQL
~~~

- [ ] **Step 2: 运行测试确认红灯**

Run: .venv/bin/python -m pytest tests/test_db_provisioner_sql.py -q

Expected: FAIL，提示 SQL 文件不存在。

- [ ] **Step 3: 实现 MySQL 5.7 SQL**

SQL 必须先创建 db_control、删除旧过程，再创建 root@localhost 定义的 SQL SECURITY DEFINER 过程：

~~~sql
CREATE DATABASE IF NOT EXISTS db_control CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE db_control;
DROP PROCEDURE IF EXISTS create_project_database;
DELIMITER //
CREATE DEFINER='root'@'localhost' PROCEDURE create_project_database(
  IN p_database VARCHAR(29),
  IN p_application_user VARCHAR(33),
  IN p_application_password VARCHAR(128)
)
SQL SECURITY DEFINER
BEGIN
  DECLARE v_database_exists INT DEFAULT 0;
  DECLARE v_user_exists INT DEFAULT 0;
  DECLARE v_database_created BOOLEAN DEFAULT FALSE;
  DECLARE v_user_created BOOLEAN DEFAULT FALSE;
  DECLARE v_sql TEXT;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    IF v_user_created THEN
      SET v_sql = CONCAT('DROP USER ', QUOTE(p_application_user), '@', QUOTE('%'));
      PREPARE cleanup_user FROM v_sql;
      EXECUTE cleanup_user;
      DEALLOCATE PREPARE cleanup_user;
    END IF;
    IF v_database_created THEN
      SET v_sql = CONCAT('DROP DATABASE `', p_database, '`');
      PREPARE cleanup_database FROM v_sql;
      EXECUTE cleanup_database;
      DEALLOCATE PREPARE cleanup_database;
    END IF;
    RESIGNAL;
  END;
  IF p_database IS NULL OR p_database NOT REGEXP '^[a-z][a-z0-9_]{2,28}$' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid database name';
  END IF;
  IF p_application_user IS NULL OR p_application_user <> CONCAT(p_database, '_app') THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid application user';
  END IF;
  IF p_application_password IS NULL OR CHAR_LENGTH(p_application_password) < 20
      OR CHAR_LENGTH(p_application_password) > 128
      OR LOCATE(CHAR(0), p_application_password) > 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'invalid application password';
  END IF;
  SELECT COUNT(*) INTO v_database_exists
    FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = p_database;
  SELECT COUNT(*) INTO v_user_exists
    FROM mysql.user WHERE User = p_application_user AND Host = '%';
  IF v_database_exists > 0 OR v_user_exists > 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'database or user already exists';
  END IF;
  SET v_sql = CONCAT('CREATE DATABASE `', p_database,
                     '` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci');
  PREPARE create_database FROM v_sql;
  EXECUTE create_database;
  DEALLOCATE PREPARE create_database;
  SET v_database_created = TRUE;
  SET v_sql = CONCAT('CREATE USER ', QUOTE(p_application_user), '@', QUOTE('%'),
                     ' IDENTIFIED BY ', QUOTE(p_application_password));
  PREPARE create_user FROM v_sql;
  EXECUTE create_user;
  DEALLOCATE PREPARE create_user;
  SET v_user_created = TRUE;
  SET v_sql = CONCAT('GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, ',
                     'CREATE TEMPORARY TABLES, LOCK TABLES, REFERENCES ON `', p_database,
                     '`.* TO ', QUOTE(p_application_user), '@', QUOTE('%'));
  PREPARE grant_project FROM v_sql;
  EXECUTE grant_project;
  DEALLOCATE PREPARE grant_project;
  FLUSH PRIVILEGES;
  SELECT p_database AS database_name, p_application_user AS application_user;
END//
DELIMITER ;
~~~

过程用 SIGNAL SQLSTATE '45000' 报告非法名称、账号不匹配、密码长度不合法和同名资源存在。项目账号固定授予 SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, CREATE TEMPORARY TABLES, LOCK TABLES, REFERENCES，目标只能是新库的所有表。

- [ ] **Step 4: 运行 SQL 契约测试并做语法预检**

Run: .venv/bin/python -m pytest tests/test_db_provisioner_sql.py -q && git diff --check

Expected: PASS；无空白错误。

- [ ] **Step 5: 提交 SQL 过程**

~~~bash
git add sql/db_provisioner.sql tests/test_db_provisioner_sql.py
git commit -m "feat: add restricted mysql provisioning procedure"
~~~

### Task 3: 服务器 root 安装脚本与安全凭据交付

**Files:**
- Create: scripts/install-db-provisioner.sh
- Modify: tests/test_db_provisioner_sql.py

**Interfaces:**
- Consumes: --source-host <IPv4>、--provisioner-password-file <absolute-path> 和同目录的 sql/db_provisioner.sql。
- Produces: 指定来源 IP 的 db_provisioner，仅拥有 EXECUTE ON PROCEDURE db_control.create_project_database。
- Side effect: 在服务器 /root/db-credentials/db_provisioner.env 保存 root-only provisioner 连接凭据。

- [ ] **Step 1: 写入安装脚本失败测试**

~~~python
INSTALLER = Path("scripts/install-db-provisioner.sh").read_text(encoding="utf-8")

def test_installer_uses_root_client_config_and_never_grants_wildcard_access():
    assert "--defaults-file=/root/.my.cnf" in INSTALLER
    assert "--source-host" in INSTALLER
    assert "'db_provisioner'@'%'" not in INSTALLER
    assert "GRANT EXECUTE ON PROCEDURE db_control.create_project_database" in INSTALLER
~~~

- [ ] **Step 2: 运行测试确认红灯**

Run: .venv/bin/python -m pytest tests/test_db_provisioner_sql.py -q

Expected: FAIL，提示安装脚本不存在。

- [ ] **Step 3: 实现安装脚本**

~~~bash
#!/usr/bin/env bash
set -euo pipefail

mysql_bin=/www/server/mysql/bin/mysql
root_defaults=/root/.my.cnf
script_dir="$(cd "$(dirname "$0")" && pwd)"
sql_file="$script_dir/../sql/db_provisioner.sql"
source_host=""
password_file=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-host) source_host="$2"; shift 2 ;;
    --provisioner-password-file) password_file="$2"; shift 2 ;;
    *) printf 'unknown argument: %s\\n' "$1" >&2; exit 64 ;;
  esac
done
[[ "$source_host" =~ ^([0-9]{1,3}\\.){3}[0-9]{1,3}$ ]] || { echo 'invalid source host' >&2; exit 64; }
[[ -r "$password_file" && -s "$password_file" ]] || { echo 'password file is unavailable' >&2; exit 66; }
permissions="$(stat -c '%a' "$password_file")"
(( (8#$permissions & 077) == 0 )) || { echo 'password file must be 0600' >&2; exit 77; }
[[ -r "$root_defaults" && -r "$sql_file" ]] || { echo 'root config or SQL file is unavailable' >&2; exit 66; }

IFS= read -r provisioner_password < "$password_file"
escaped_password="${provisioner_password//\\\\/\\\\\\\\}"
escaped_password="${escaped_password//\'/\\\'}"
"$mysql_bin" --defaults-file="$root_defaults" < "$sql_file"
"$mysql_bin" --defaults-file="$root_defaults" <<SQL
DROP USER IF EXISTS 'db_provisioner'@'%';
DROP USER IF EXISTS 'db_provisioner'@'${source_host}';
CREATE USER 'db_provisioner'@'${source_host}' IDENTIFIED BY '${escaped_password}';
GRANT EXECUTE ON PROCEDURE db_control.create_project_database TO 'db_provisioner'@'${source_host}';
FLUSH PRIVILEGES;
SQL
install -d -m 700 /root/db-credentials
umask 077
printf 'MYSQL_PROVISIONER_HOST=64.83.38.223\\nMYSQL_PROVISIONER_PORT=3306\\nMYSQL_PROVISIONER_USER=db_provisioner\\nMYSQL_PROVISIONER_PASSWORD=%s\\n' "$provisioner_password" > /root/db-credentials/db_provisioner.env
rm -f "$password_file"
printf 'provisioner_user=db_provisioner\\nsource_host=%s\\nresult=installed\\n' "$source_host"
~~~

标准输出只能包含账号、来源 IP 和结果；不得回显密码。

- [ ] **Step 4: 验证脚本**

Run: bash -n scripts/install-db-provisioner.sh && .venv/bin/python -m pytest tests/test_db_provisioner_sql.py -q

Expected: shell 语法与测试均通过。

- [ ] **Step 5: 安装到服务器并执行集成验证**

以服务器 root 身份把 sql/db_provisioner.sql 和 scripts/install-db-provisioner.sh 复制到 /root/db-provisioner-install/。在服务器生成 32 字节随机 provisioner 密码文件（0600），再执行：

~~~bash
/root/db-provisioner-install/install-db-provisioner.sh \
  --source-host 58.38.101.149 \
  --provisioner-password-file /root/db-credentials/db_provisioner-password
~~~

安全复制服务器的 root-only provisioner 凭据文件到本地 .provisioning/db_provisioner.env（保留 0600），不得输出文件内容。使用 Task 1 CLI 创建 provision_check_20260730，验证独立账号可在自己的库建表、无法读取 knowledge，随后用 root 清理该测试库和账号。

- [ ] **Step 6: 提交安装脚本**

~~~bash
git add scripts/install-db-provisioner.sh tests/test_db_provisioner_sql.py
git commit -m "feat: add restricted database provisioner installer"
~~~

### Task 4: 中文操作手册与完整验证

**Files:**
- Create: docs/服务器MySQL自助建库操作手册.md

**Interfaces:**
- Consumes: .provisioning/db_provisioner.env 与 scripts/create_project_database.py。
- Produces: 面向本地项目的安全开库、连接和故障处理指引。

- [ ] **Step 1: 编写操作手册**

手册必须包含：

1. 凭据文件位置和 chmod 600 要求；
2. 创建命令：

~~~bash
set -a
source .provisioning/db_provisioner.env
set +a
.venv/bin/python scripts/create_project_database.py \
  --project my_project \
  --credentials-file .project-databases/my_project.env
~~~

3. 在其他项目配置 MYSQL_HOST、MYSQL_PORT、MYSQL_DATABASE、MYSQL_USER、MYSQL_PASSWORD；
4. 禁止提交 .provisioning/ 和 .project-databases/；
5. 同名库、来源 IP 变化、连接失败的排查；
6. 删除数据库只能由服务器 root 单独执行。

- [ ] **Step 2: 运行完整测试与真实连接验证**

Run:

~~~bash
.venv/bin/python -m pytest -q
bash -n scripts/install-db-provisioner.sh
.venv/bin/python scripts/create_project_database.py \
  --project manual_verify_20260730 \
  --credentials-file .project-databases/manual_verify_20260730.env
~~~

Expected: 全部 pytest 通过；CLI 仅输出非敏感信息；服务器上新库与独立账号可连接。完成验证后，用 root 删除 manual_verify_20260730 与对应账号。

- [ ] **Step 3: 提交手册**

~~~bash
git add docs/服务器MySQL自助建库操作手册.md
git commit -m "docs: add mysql self-service provisioning guide"
~~~
