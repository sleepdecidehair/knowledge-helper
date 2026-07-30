# 本地项目自助创建服务器 MySQL 数据库设计

## 目标

让本地项目在已连接服务器 MySQL 的前提下，自助创建一个新的项目数据库、独立应用账号和独立密码，而不把 MySQL `root` 或全局管理权限交给项目代码。

## 范围

- 服务器新增一个受限的 `db_provisioner` MySQL 账号。
- 新增一个 `SQL SECURITY DEFINER` 的受控存储过程 `db_control.create_project_database`。
- 本地项目使用 `db_provisioner` 调用该过程创建项目资源。
- 本次不迁移现有 `knowledge` 或 `nuo_car` 数据库，不更改知识库应用的 `kh_user` 权限。

## 调用约定

本地项目生成自己的高强度应用密码，并通过下列调用提交：

```sql
CALL db_control.create_project_database(
  'project_name',
  'project_name_app',
  'generated_application_password'
);
```

成功时返回数据库名和应用账号。调用方负责把密码保存在自身的未提交 `.env` 或密钥管理系统中；服务端不回显或持久化应用密码。

## 权限模型

| 主体 | 权限 |
| --- | --- |
| `db_provisioner` | 仅拥有 `EXECUTE` 调用该过程的权限；无任意库读写、建库、建用户或授权权限。 |
| 过程定义者 | 仅用于执行过程内部受控的建库、建用户和按库授权 SQL。 |
| `<project>_app` | 仅拥有 `<project>.*` 的业务读写、建表、迁移和索引权限；没有 `GRANT OPTION`、全局权限或其他库权限。 |

`db_provisioner` 的密码仅保存在本地开发环境的忽略文件中。MySQL 账号限制为项目使用者的固定公网 IP；若公网 IP 变更，需显式更新白名单，不能退化为任意公网来源。

## 输入校验与幂等

- 数据库名仅允许 `^[a-z][a-z0-9_]{2,28}$`。
- 应用账号必须严格等于 `${database}_app`，并满足 MySQL 账号长度限制。
- 应用密码长度必须在 20 到 128 个字符之间，且不能包含 NUL 字符。
- 数据库或应用账号已存在时，过程以明确错误退出，绝不覆盖、删除或重置既有资源。
- 动态 SQL 仅使用经过白名单校验的标识符；密码通过 MySQL 转义函数安全嵌入。

## 数据流

1. 本地项目生成应用密码，并用 `db_provisioner` 连接服务器 MySQL。
2. 项目调用 `db_control.create_project_database`。
3. 存储过程校验参数、检查同名库和账号不存在。
4. 过程创建数据库、应用账号，并授予该账号对新库的最小业务权限。
5. 过程返回非敏感连接信息；本地项目使用新应用账号验证连接。

## 错误与安全处理

- 输入不合法、资源已存在、权限不足或建库失败均返回 SQL 错误，不做半覆盖处理。
- MySQL 的建库、建用户和授权属于不可回滚的 DDL。过程记录本次调用中已创建的资源；后续步骤失败时，只反向清理本次新建的账号和数据库，绝不触碰调用前已存在的资源。
- MySQL `root` 密码和 `db_provisioner` 密码不写入 Git、日志或聊天。
- 只允许经白名单公网 IP 使用 `db_provisioner`；同时保留服务器防火墙对 3306 的来源限制作为第二层防护。

## 验收

1. 使用 `db_provisioner` 能创建一个符合规则的新项目库和独立应用账号。
2. 新应用账号可连接自己的库、建表和读写数据。
3. 新应用账号无法访问 `knowledge`、`nuo_car` 或其他项目库。
4. `db_provisioner` 无法直接执行 `CREATE DATABASE`、`CREATE USER` 或读取业务表。
5. 非白名单来源无法连接 `db_provisioner`。
6. 对同名项目重复调用不会覆盖已有数据库或账号。
