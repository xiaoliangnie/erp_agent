# 已下线的旧前端（可删）

前端改成 Vite + React 单页应用后，这些文件不在任何链路上，留在这里只为对照口径。
确认新页面无缺失后可以整个目录删掉。

- `frontend-html/*.html`：五个独立页面的旧实现，CSS 与 JS 内联在文件里。
  对应的 React 实现在 `frontend/src/pages/`。
- `frontend-html/js/integration-adapters.js`：旧的「先打同源 API，失败回退全局变量」适配层。
  新前端只走 API，见 `frontend/src/api/client.ts`。
- `frontend-html/data/*.js`：离线快照生成物（`window.PO_DATA` / `window.DELIV`）。
- `scripts/build_data.py`、`scripts/build_delivery_data.py`：上面两个生成物的生成器。
  它们是 `backend/procurement_data.py` 同一套转换的第二份实现，砍掉离线链路后不再需要
  维护两边一致。
- `scripts/seed_realtime_mirror.py`：把旧的只读采购镜像（`10004_jst_purchase-main` 两张表）
  一次性灌进规范化实时镜像表。历史数据已经灌完，镜像库现在只由聚水潭 API 增量维护
  （`backend/realtime_mirror.py`），这个脚本的来源库已下线。

`data/snapshots/采购单完整数据.csv` 仍留在原处，作为字段口径的参照样本。
