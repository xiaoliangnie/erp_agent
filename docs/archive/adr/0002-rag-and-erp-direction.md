# ADR 0002：RAG 边界与 ERP AI 账号方向

## RAG

旧决策：不做 RAG。实时数字禁止向量检索。

状态：SUPERSEDED（部分）

新决策：

- 实时 ERP 数字（库存、价格、采购单状态、付款、准交率）**继续 Exact Query**，禁止 RAG。
- 制度、SOP、供应商资料、历史报价附件、合同条款可以在以后走 RAG。
- 本轮只留 `RetrievalRouter` 接口，不部署向量库 / Neo4j。

## ERP 自动操作

旧决策：服务端不持有 ERP 登录态。

状态：SUPERSEDED

新决策：

- 未来后端使用专用 AI ERP 服务账号，凭证进 `.env` / Secret。
- 员工不需要自己保持 ERP 页面登录。
- 所有写操作仍是 Typed ERP Command（换货、以后的建单/出库），禁止 `open_browser` / `click_anything` / `execute_js`。
- 写操作审计至少包含：员工 `user_id`、工具、AI 账号、命令、前后状态、时间、结果。优先扩展现有 `tool_executions` / `exchange_jobs`。

当前本机 Digital Worker 登录 cookie 仍落 `files/data/secrets/erp-ai-state.json`，这是过渡形态，不是通用 Browser Agent。
