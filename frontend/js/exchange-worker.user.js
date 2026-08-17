// 已退役：换货 / 探测 / 搜 SKU / 图片同步改由后端 Playwright DigitalWorkerLoop 执行。
// 本脚本若仍安装，服务端领取接口会返回空（executor=backend）。
// ==UserScript==
// @name         采购 Agent · 聚水潭订单换货 Worker
// @namespace    procurement-agent
// @version      0.8.0
// @description  在已登录的聚水潭订单页读取订单、dry-run，并仅在网页人工确认后执行 SKU 更换；同时暴露 JstOrderExchange 供控制台 / Codex 直接调用
// @match        *://*/app/order/order/list.aspx*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @connect      *
// ==/UserScript==

(function () {
  'use strict';

  const VERSION = '0.8.0';
  const DEFAULT_SERVER = 'http://127.0.0.1:8777';
  const POLL_MS = 3000;
  const WRITE_DELAY_MS = 250;
  let busy = false;
  let corePromise = null;

  // Tampermonkey 默认把 userscript 放在隔离环境；聚水潭的 _ACP、_CallPage、jTable
  // 则定义在页面环境。所有 ERP 页面对象都从 unsafeWindow 读取。
  const erpWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

  // 每个 ERP 标签页都是一个独立并行槽位。ID 不跨标签页持久化，旧页面停止心跳后
  // 会在 45 秒内自然离线；避免多个标签页共享 GM 存储中的同一个 Worker ID。
  const workerId = 'erp-' + crypto.randomUUID();

  function config() {
    return {
      server: String(GM_getValue('server', DEFAULT_SERVER)).replace(/\/$/, ''),
      token: String(GM_getValue('token', '')),
    };
  }

  function configure() {
    const current = config();
    const server = prompt('采购 Agent 服务地址', current.server);
    if (server === null) return;
    const token = prompt('EXCHANGE_WORKER_TOKEN（保存在油猴脚本存储中）', current.token);
    if (token === null) return;
    GM_setValue('server', server.trim().replace(/\/$/, ''));
    GM_setValue('token', token.trim());
    alert('换货 Worker 配置已保存。');
  }

  GM_registerMenuCommand('配置服务地址和 Worker Token', configure);
  GM_registerMenuCommand('显示 Worker ID', () => alert(workerId));
  GM_registerMenuCommand('重新加载换货核心', () => {
    corePromise = null;
    if (erpWindow.JstOrderExchange) delete erpWindow.JstOrderExchange;
    ensureCore().then((core) => {
      alert('核心已加载 version=' + (core && core.version));
    }).catch((error) => alert(String(error)));
  });

  function api(method, path, body) {
    const cfg = config();
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: cfg.server + path,
        headers: {
          Authorization: 'Bearer ' + cfg.token,
          'Content-Type': 'application/json',
        },
        data: body === undefined ? undefined : JSON.stringify(body),
        timeout: 20000,
        onload(response) {
          let value;
          try { value = JSON.parse(response.responseText || '{}'); }
          catch (_) { return reject(new Error('服务返回了非 JSON 内容')); }
          if (response.status < 200 || response.status >= 300) {
            return reject(new Error(value.error || ('HTTP ' + response.status)));
          }
          resolve(value);
        },
        onerror: () => reject(new Error('无法连接采购 Agent 服务')),
        ontimeout: () => reject(new Error('采购 Agent 服务请求超时')),
      });
    });
  }

  function gmText(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET', url, timeout: 20000,
        onload(response) {
          if (response.status < 200 || response.status >= 300) {
            return reject(new Error('下载核心脚本 HTTP ' + response.status));
          }
          resolve(response.responseText || '');
        },
        onerror: () => reject(new Error('下载核心脚本失败')),
        ontimeout: () => reject(new Error('下载核心脚本超时')),
      });
    });
  }

  function gmBinary(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET', url, responseType: 'arraybuffer', timeout: 20000,
        onload(response) {
          if (response.status < 200 || response.status >= 300) {
            return reject(new Error('图片下载 HTTP ' + response.status));
          }
          const typeMatch = String(response.responseHeaders || '').match(/^content-type:\s*([^;\r\n]+)/im);
          resolve({bytes: response.response, mimeType: typeMatch ? typeMatch[1].toLowerCase() : ''});
        },
        onerror: () => reject(new Error('图片下载失败')),
        ontimeout: () => reject(new Error('图片下载超时')),
      });
    });
  }

  function base64(arrayBuffer) {
    const bytes = new Uint8Array(arrayBuffer);
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary);
  }

  function absoluteErpUrl(value) {
    const source = String(value || '').trim();
    if (!source) return '';
    try { return new URL(source, location.origin).href; }
    catch (_) { return ''; }
  }

  /** 把 core 注入页面上下文，使控制台 / Codex evaluate 都能拿到 JstOrderExchange。 */
  function injectScript(source) {
    return new Promise((resolve, reject) => {
      try {
        const el = document.createElement('script');
        el.textContent = source + '\n//# sourceURL=jst-order-exchange.core.js';
        (document.documentElement || document.head || document.body).appendChild(el);
        el.remove();
        if (!erpWindow.JstOrderExchange) {
          return reject(new Error('核心脚本已注入但未挂上 JstOrderExchange'));
        }
        resolve(erpWindow.JstOrderExchange);
      } catch (error) {
        reject(error);
      }
    });
  }

  async function ensureCore() {
    if (erpWindow.JstOrderExchange && typeof erpWindow.JstOrderExchange.plan === 'function') {
      return erpWindow.JstOrderExchange;
    }
    if (corePromise) return corePromise;
    corePromise = (async () => {
      const cfg = config();
      const url = cfg.server + '/js/jst-order-exchange.core.js?v=' + encodeURIComponent(VERSION);
      const source = await gmText(url);
      return injectScript(source);
    })().catch((error) => {
      corePromise = null;
      throw error;
    });
    return corePromise;
  }

  async function purchaseItems(poId) {
    const query = new URLSearchParams(location.search);
    const owner = query.get('owner_co_id') || query.get('authorize_co_id') || '10235039';
    const url = new URL('/app/scm/purchase/purchaseitem.aspx', location.origin);
    Object.entries({
      po_id: String(poId), p_co_id: owner, p_owner_co_id: owner, all_data: 'true', archive: 'false',
      owner_co_id: owner, authorize_co_id: owner,
    }).forEach(([key, value]) => url.searchParams.set(key, value));
    const response = await fetch(url.href, {credentials: 'include'});
    if (!response.ok) throw new Error('采购明细接口 HTTP ' + response.status);
    const html = await response.text();
    const documentValue = new DOMParser().parseFromString(html, 'text/html');
    const node = documentValue.querySelector('#_jt_data');
    if (!node) throw new Error('采购明细接口没有返回 #_jt_data，可能登录已失效');
    const payload = JSON.parse(node.textContent || '{}');
    if (!Array.isArray(payload.datas)) throw new Error('采购明细接口 datas 格式不正确');
    return payload.datas;
  }

  function imageUrl(item) {
    for (const field of ['pic300', 'pic160', 'pic100', 'pic60', 'pic30', 'pic']) {
      const found = absoluteErpUrl(item && item[field]);
      if (found) return found;
    }
    return '';
  }

  async function syncImages(job) {
    const failed = [];
    let rows;
    try { rows = await purchaseItems(job.purchaseOrderNo); }
    catch (error) {
      await api('POST', `/api/exchange/worker/images/${job.id}/result`, {
        workerId, result: {failed: job.targets, error: String(error)},
      });
      return;
    }
    for (const target of job.targets) {
      try {
        const item = rows.find(row => String(row.sku_id || '') === String(target.sku));
        if (!item) throw new Error('采购明细接口未找到该 SKU');
        const sourceUrl = imageUrl(item);
        if (!sourceUrl) throw new Error('采购明细接口没有 pic300/pic160/pic100');
        const downloaded = await gmBinary(sourceUrl);
        let mimeType = downloaded.mimeType;
        if (!['image/png', 'image/jpeg', 'image/webp'].includes(mimeType)) {
          const pathname = new URL(sourceUrl).pathname.toLowerCase();
          mimeType = pathname.endsWith('.png') ? 'image/png' : pathname.endsWith('.webp') ? 'image/webp' : 'image/jpeg';
        }
        await api('POST', `/api/exchange/worker/images/${job.id}/upload`, {
          workerId, sku: target.sku, sourceUrl, mimeType, imageBase64: base64(downloaded.bytes),
        });
      } catch (error) {
        failed.push({sku: target.sku, error: String(error)});
      }
    }
    await api('POST', `/api/exchange/worker/images/${job.id}/result`, {
      workerId, result: {failed, attempted: job.targets.length},
    });
  }

  function ready() {
    return typeof erpWindow._ACP === 'function' && /\/app\/order\/order\/list\.aspx/i.test(location.pathname);
  }

  async function buildPlan(job) {
    const core = await ensureCore();
    return core.planJob(job);
  }

  async function execute(job) {
    const token = job.executionToken;
    const core = await ensureCore();
    const result = await core.executeJob(job, {
      confirm: true,
      delayMs: WRITE_DELAY_MS,
      onProgress: async (event) => {
        try {
          await api('POST', `/api/exchange/worker/jobs/${job.id}/progress`, {
            workerId, executionToken: token, event,
          });
        } catch (error) {
          console.warn('[采购 Agent 换货 Worker] 进度上报失败', error);
        }
      },
    });
    await api('POST', `/api/exchange/worker/jobs/${job.id}/result`, {
      workerId, executionToken: token,
      result: result,
    });
  }

  async function tick() {
    const cfg = config();
    if (busy || !cfg.token) return;
    busy = true;
    try {
      // 尽早挂上页面 API，方便控制台 / Codex 调用（不依赖领到任务）。
      if (ready()) {
        try { await ensureCore(); } catch (error) {
          console.warn('[采购 Agent 换货 Worker] 核心加载失败', error);
        }
      }
      await api('POST', '/api/exchange/worker/heartbeat', {
        workerId, version: VERSION, pageUrl: location.href, ready: ready(),
        detail: {
          title: document.title,
          hasAcp: typeof erpWindow._ACP === 'function',
          hasJTable: typeof erpWindow.jTable !== 'undefined',
          hasCore: !!(erpWindow.JstOrderExchange && erpWindow.JstOrderExchange.version),
          coreVersion: erpWindow.JstOrderExchange && erpWindow.JstOrderExchange.version,
        },
      });
      if (!ready()) return;
      // 换货任务优先于图片/探测等辅助队列。服务端会先发已人工确认的 execute，
      // 再发新的 dry-run；多标签页会各自领取一个不同任务并行处理。
      const next = await api('GET', '/api/exchange/worker/jobs/next?worker_id=' + encodeURIComponent(workerId));
      if (next.job) {
        if (next.job.action === 'plan') {
          const plan = await buildPlan(next.job);
          await api('POST', `/api/exchange/worker/jobs/${next.job.id}/plan`, {workerId, plan});
        } else if (next.job.action === 'execute') {
          await execute(next.job);
        }
        return;
      }
      const probeNext = await api('GET', '/api/exchange/worker/probes/next?worker_id=' + encodeURIComponent(workerId));
      if (probeNext.probe) {
        let result;
        try {
          if (probeNext.probe.kind !== 'purchase_items') throw new Error('不支持的只读探测类型');
          const rows = await purchaseItems(probeNext.probe.reference);
          result = {poId: probeNext.probe.reference, count: rows.length, items: rows};
        } catch (error) { result = {error: String(error)}; }
        await api('POST', `/api/exchange/worker/probes/${probeNext.probe.id}/result`, {workerId, result});
        return;
      }
      const searchNext = await api('GET', '/api/exchange/worker/searches/next?worker_id=' + encodeURIComponent(workerId));
      if (searchNext.search) {
        let result;
        try {
          const core = await ensureCore();
          result = await core.searchSku({sku: searchNext.search.sku, limit: 500});
        } catch (error) {
          result = {error: String(error)};
        }
        await api('POST', `/api/exchange/worker/searches/${searchNext.search.id}/result`, {workerId, result});
        return;
      }
      const imageNext = await api('GET', '/api/exchange/worker/images/next?worker_id=' + encodeURIComponent(workerId));
      if (imageNext.job) {
        await syncImages(imageNext.job);
        return;
      }
    } catch (error) {
      console.warn('[采购 Agent 换货 Worker]', error);
    } finally {
      busy = false;
    }
  }

  console.info('[采购 Agent 换货 Worker] 已加载', {workerId, ready: ready(), version: VERSION});
  tick();
  setInterval(tick, POLL_MS);
})();
