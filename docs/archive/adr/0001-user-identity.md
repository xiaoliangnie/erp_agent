# ADR 0001：users 是身份事实主体

## 旧决策

`staff_bindings` 用采购员姓名当身份枢纽。`RequestContext.user_id` 在有钉钉绑定时等于钉钉 userId，否则 `staff:<姓名>`。

状态：SUPERSEDED

## 新决策

`users.user_id`（`usr_...`）是内部永久身份。Web / 钉钉 / ERP 采购员别名都映射到它。

`staff_bindings` 降为兼容层：姓名 ↔ 钉钉 userId ↔ `users.user_id`。`operator` 自由文本保留，不删。

解析顺序：

1. bindings 上已回填的 `user_id`
2. `users` 按钉钉 / 采购员别名命中
3. 回退钉钉 userId / `staff:<姓名>` / `cli:<姓名>`

不得静默建用户。不能确定的别名进 `needs_review`。

## 原因

外部身份会变；审计、确认、记忆、ERP 命令必须长期稳定。不能再把「张三」或钉钉 userId 当主键。

## 后果

- Agent SQLite 新增 `users`，不改镜像库。
- 旧会话审计里可能仍是钉钉 userId；新请求在 seed 之后用 `usr_`。
- 完整 IAM / SSO 仍是 `[DEFERRED]`。
