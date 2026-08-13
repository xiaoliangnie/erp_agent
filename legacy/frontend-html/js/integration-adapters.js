/*
 * 采购看板集成适配器。
 *
 * 数据源优先调用同源后端 API；服务未启动时回退到本地生成数据。
 * 接 Agent、邮件或群聊时，将 reminder.configured 设为 true 并实现
 * send(batch)。不要在浏览器代码中保存数据库密码或邮件凭证。
 */
(function () {
  "use strict";

  const existing = window.ProcurementAdapters || {};
  async function getJSON(path, context) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const year = context && context.requestedYear;
      const url = year ? path + "?year=" + encodeURIComponent(year) : path;
      const response = await fetch(url, {
        signal: controller.signal,
        headers: { "Accept": "application/json" }
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function apiOrFallback(path, fallback, context) {
    try {
      return await getJSON(path, context);
    } catch (error) {
      console.info("采购数据库接口不可用，已使用本地数据快照。", error.message);
      return fallback || null;
    }
  }

  const dataSource = Object.assign({
    async getDashboardData(context) {
      return apiOrFallback("/api/dashboard", window.PO_DATA, context);
    },
    async getDeliveryData(context) {
      return apiOrFallback("/api/delivery", window.DELIV, context);
    }
  }, existing.dataSource || {});

  const reminder = Object.assign({
    configured: false,
    async send(batch) {
      return {
        ok: false,
        code: "REMINDER_ADAPTER_NOT_CONFIGURED",
        message: "提醒接口已预留，尚未接入 Agent、邮件或群聊服务。",
        batch: batch
      };
    }
  }, existing.reminder || {});

  window.ProcurementAdapters = Object.assign(existing, { dataSource, reminder });
})();
