/**
 * 聚水潭订单 SKU 换货 · 页内核心（可注入 / 可被 Codex·Playwright 直接调用）
 *
 * 必须在已登录的 ERP 订单列表页执行：
 *   /app/order/order/list.aspx
 *
 * 依赖页面原生对象：_ACP、_CallPage、jTable（可选）
 *
 * 暴露：
 *   globalThis.JstOrderExchange = {
 *     version, ready, loadOrder, planOrder, plan, execute, changeItem
 *   }
 *
 * 用法（浏览器控制台 / 油猴 / Codex page.evaluate）：
 *   const dry = await JstOrderExchange.plan({
 *     oIds: ['10001', '10002'],
 *     from: 'OLD-SKU',
 *     to: 'NEW-SKU',
 *     sourceStyle: 'STYLE',   // 同款换货必填
 *     targetStyle: 'STYLE',
 *     exchangeType: 'same_style', // 或 special_mapping
 *   });
 *   // 核对 dry.plans 后：
 *   const result = await JstOrderExchange.execute({
 *     plans: dry.plans.filter(p => p.ok),
 *     confirm: true,          // 必须显式 true，否则拒绝写操作
 *     delayMs: 250,
 *   });
 */
(function (root) {
  'use strict';

  const VERSION = '0.6.0';
  const DEFAULT_WRITE_DELAY_MS = 250;
  const DEFAULT_FORBIDDEN = '取消|退款|关闭|Cancelled|Delete|Merged';

  function erp() {
    return root;
  }

  function ready() {
    const w = erp();
    return typeof w._ACP === 'function'
      && /\/app\/order\/order\/list\.aspx/i.test(String(w.location && w.location.pathname || ''));
  }

  function acp(method) {
    const w = erp();
    const args = Array.prototype.slice.call(arguments, 1);
    return new Promise(function (resolve, reject) {
      try {
        w._ACP.apply(w, [method, function (value) { resolve(value); }].concat(args));
      } catch (error) {
        reject(error);
      }
    });
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function currentOrder(oid) {
    const w = erp();
    const rows = w.jTable && w.jTable.Data && w.jTable.Data.datas;
    return Array.isArray(rows) ? rows.find(function (row) { return String(row.o_id) === String(oid); }) : null;
  }

  function normalizeReload(value) {
    let source = value;
    if (Array.isArray(source)) source = source[0];
    if (typeof source === 'string') {
      try { source = JSON.parse(source); } catch (_) { return null; }
    }
    return source && (source.data || source);
  }

  async function loadOrder(oid) {
    const w = erp();
    let order = currentOrder(oid);
    let items = order && order.items;
    if (typeof w._CallPage === 'function') {
      try {
        const reloaded = normalizeReload(w._CallPage('ReloadOrdersV2', String(oid), true));
        if (reloaded && String(reloaded.o_id || oid) === String(oid)) {
          order = Object.assign({}, order || {}, reloaded);
          items = reloaded.items || items;
        }
      } catch (error) {
        if (!order) throw new Error('读取订单 ' + oid + ' 失败：' + String(error));
      }
    }
    if (!order) {
      return { o_id: String(oid), items: [], load_error: 'ERP 未返回该订单' };
    }
    return Object.assign({}, order, {
      o_id: String(oid),
      items: Array.isArray(items) ? items : [],
    });
  }

  /** 只读扫描当前 ERP 订单数据集，并逐单读取明细反查 SKU。 */
  async function searchSku(input) {
    input = input || {};
    if (!ready()) throw new Error('ERP 页面未就绪');
    const sku = String(input.sku || '').trim();
    if (!sku) throw new Error('搜索 SKU 不能为空');
    const rows = erp().jTable && erp().jTable.Data && erp().jTable.Data.datas;
    if (!Array.isArray(rows)) throw new Error('ERP 当前页没有可读取的订单数据集');
    const maxOrders = Math.max(1, Math.min(Number(input.limit) || 500, 500));
    const candidates = [];
    rows.forEach(function (row) {
      const oid = String(row && row.o_id || '').trim();
      if (oid && !candidates.some(function (item) { return item.o_id === oid; }) && candidates.length < maxOrders) {
        candidates.push({
          o_id: oid,
          so_id: String(row.so_id || row.outer_so_id || ''),
          status: String(row.status || ''),
          shop_name: String(row.shop_name || ''),
        });
      }
    });
    const matches = [];
    const failures = [];
    for (let index = 0; index < candidates.length; index += 1) {
      const candidate = candidates[index];
      try {
        const order = await loadOrder(candidate.o_id);
        const lines = (order.items || []).filter(function (item) { return skuOf(item) === sku; });
        if (lines.length) {
          matches.push({
            o_id: candidate.o_id,
            so_id: String(order.so_id || order.outer_so_id || candidate.so_id || ''),
            status: String(order.status || candidate.status || ''),
            shop_name: String(order.shop_name || candidate.shop_name || ''),
            sku: sku,
            quantity: lines.reduce(function (sum, item) { return sum + qtyOf(item); }, 0),
            line_count: lines.length,
          });
        }
      } catch (error) {
        failures.push({o_id: candidate.o_id, error: String(error)});
      }
    }
    return {
      sku: sku,
      scope: 'current_jtable_dataset',
      scannedOrders: candidates.length,
      matchedOrders: matches.length,
      orders: matches,
      failures: failures.slice(0, 50),
    };
  }

  function skuOf(item) {
    return String((item && (item.sku_id || item.skuId)) || '').trim();
  }

  function styleOf(item) {
    return String((item && (item.i_id || item.iId || item.style_id)) || '').trim();
  }

  function qtyOf(item) {
    const value = Number(item && (item.qty ?? item.qty_count ?? item.orderQty ?? 0));
    return Number.isFinite(value) ? value : 0;
  }

  function buildRules(input) {
    const from = String(input.from || input.src_sku_id || '').trim();
    const to = String(input.to || input.new_sku_id || '').trim();
    if (!from || !to) throw new Error('from / to（源 SKU / 目标 SKU）不能为空');
    if (from === to) throw new Error('源 SKU 与目标 SKU 不能相同');
    const sourceStyle = String(input.sourceStyle || input.source_style || '').trim();
    const targetStyle = String(input.targetStyle || input.target_style || '').trim();
    const exchangeType = String(input.exchangeType || input.exchange_type || '').trim();
    return {
      strategy: 'direct',
      forbidden_status_regex: String(input.forbiddenStatusRegex || input.forbidden_status_regex || DEFAULT_FORBIDDEN),
      replacements: [{
        from: from,
        to: to,
        sourceStyle: sourceStyle,
        targetStyle: targetStyle,
        exchangeType: exchangeType,
      }],
    };
  }

  function normalizeOids(raw) {
    let list = raw;
    if (typeof list === 'string') {
      list = list.split(/[\s,，;；]+/);
    }
    if (!Array.isArray(list)) throw new Error('oIds 必须是订单号数组或分隔字符串');
    const oids = [];
    list.forEach(function (item) {
      const oid = String(item || '').trim();
      if (oid && oids.indexOf(oid) < 0) oids.push(oid);
    });
    if (!oids.length) throw new Error('至少需要一个 o_id');
    if (oids.length > 500) throw new Error('单次最多 500 个订单号');
    return oids;
  }

  function planOrder(order, rules) {
    const oid = String(order.o_id || '');
    if (order.load_error) return { o_id: oid, ok: false, reason: order.load_error };
    if (!Array.isArray(order.items) || !order.items.length) {
      return { o_id: oid, ok: false, reason: '订单没有可读取的商品明细' };
    }
    const forbidden = new RegExp(rules.forbidden_status_regex || DEFAULT_FORBIDDEN, 'i');
    const status = String(order.status || '');
    if (forbidden.test(status)) {
      return { o_id: oid, ok: false, reason: '状态不允许：' + status };
    }
    const replacement = rules.replacements[0];
    const source = String(replacement.from);
    const target = String(replacement.to);
    const lines = order.items.filter(function (item) { return skuOf(item) === source; });
    if (!lines.length) {
      return {
        o_id: oid,
        so_id: String(order.so_id || ''),
        status: status,
        ok: false,
        reason: '未找到源 SKU（可能已经换过）',
        source_sku: source,
        target_sku: target,
      };
    }
    const sourceStyles = [];
    lines.forEach(function (item) {
      const style = styleOf(item);
      if (style && sourceStyles.indexOf(style) < 0) sourceStyles.push(style);
    });
    const sourceStyle = sourceStyles.length === 1
      ? sourceStyles[0]
      : String(replacement.sourceStyle || '');
    const targetStyle = String(replacement.targetStyle || '');
    const expectedType = String(replacement.exchangeType || '');
    const exchangeType = expectedType === 'special_mapping'
      ? 'special_mapping'
      : (sourceStyle && targetStyle && sourceStyle === targetStyle ? 'same_style' : 'unknown');
    if (expectedType === 'same_style' && exchangeType !== 'same_style') {
      return {
        o_id: oid,
        so_id: String(order.so_id || ''),
        status: status,
        ok: false,
        reason: '订单中的源 SKU 与目标 SKU 不是同一款式，已阻止换货',
        source_sku: source,
        target_sku: target,
        source_style: sourceStyle,
        target_style: targetStyle,
      };
    }
    return {
      o_id: oid,
      so_id: String(order.so_id || order.outer_so_id || ''),
      shop_name: String(order.shop_name || ''),
      status: status,
      ok: true,
      mode: 'ChangeItem',
      src_sku_id: source,
      new_sku_id: target,
      qty: lines.reduce(function (sum, item) { return sum + qtyOf(item); }, 0),
      exchange_type: exchangeType,
      source_style: sourceStyle,
      target_style: targetStyle,
      warning: exchangeType === 'special_mapping' ? '特殊白名单映射：请重点核对鞋垫尺码' : '',
      source_lines: lines.map(function (item) {
        return { oi_id: item.oi_id || null, sku_id: source, qty: qtyOf(item) };
      }),
    };
  }

  /**
   * dry-run：按订单号清单试算，不写 ERP。
   * @param {object} input
   * @param {string[]|string} input.oIds
   * @param {string} input.from
   * @param {string} input.to
   * @param {string} [input.sourceStyle]
   * @param {string} [input.targetStyle]
   * @param {string} [input.exchangeType] same_style | special_mapping
   * @param {object} [input.rules] 若提供则忽略 from/to 等简写字段（Agent 任务格式）
   * @param {string[]} [input.targets.o_ids] Agent 任务格式
   */
  async function plan(input) {
    input = input || {};
    if (!ready()) {
      throw new Error('当前页不是已就绪的聚水潭订单列表（需要 _ACP 与 list.aspx）');
    }
    let rules;
    let oids;
    if (input.rules && input.targets) {
      rules = input.rules;
      oids = normalizeOids(input.targets.o_ids || input.targets.oIds);
    } else {
      rules = input.rules || buildRules(input);
      oids = normalizeOids(input.oIds || input.o_ids || (input.targets && input.targets.o_ids));
    }
    const plans = [];
    for (let i = 0; i < oids.length; i += 1) {
      const oid = oids[i];
      try {
        plans.push(planOrder(await loadOrder(oid), rules));
      } catch (error) {
        plans.push({ o_id: String(oid), ok: false, reason: String(error) });
      }
    }
    return {
      total: plans.length,
      exchangeable: plans.filter(function (item) { return item.ok; }).length,
      skipped: plans.filter(function (item) { return !item.ok; }).length,
      plans: plans,
      rules: {
        from: rules.replacements && rules.replacements[0] && rules.replacements[0].from,
        to: rules.replacements && rules.replacements[0] && rules.replacements[0].to,
      },
    };
  }

  function changeArg(planItem) {
    return JSON.stringify({
      filter: 'checked',
      src_sku_id: planItem.src_sku_id,
      new_sku_id: planItem.new_sku_id,
      combinetype: 'normal',
      enty_sku_id: '',
      ignoreInventory: false,
      formula: '',
      randomIgnoreInventory: false,
      randomChange: false,
      skuKeep: false,
      partWarehouseInv: false,
    });
  }

  async function changeItem(planItem) {
    if (!planItem || !planItem.ok) throw new Error('只能执行 ok=true 的试算明细');
    if (planItem.mode !== 'ChangeItem') throw new Error('当前核心只支持 ChangeItem，收到：' + planItem.mode);
    if (!ready()) throw new Error('ERP 页面未就绪');
    const rv = await acp(
      'ChangeItem',
      JSON.stringify({ o_id: String(planItem.o_id) }),
      changeArg(planItem)
    );
    return {
      o_id: planItem.o_id,
      mode: planItem.mode,
      response: typeof rv === 'string' ? rv.slice(0, 500) : rv,
    };
  }

  /**
   * 真实换货。默认拒绝；必须 confirm:true。
   * @param {object} input
   * @param {object[]} input.plans  通常来自 plan() 结果中 ok=true 的项
   * @param {boolean} input.confirm 必须为 true
   * @param {number} [input.delayMs=250]
   * @param {function} [input.onProgress] (event) => void
   */
  async function execute(input) {
    input = input || {};
    if (input.confirm !== true) {
      throw new Error('拒绝执行：真实换货必须传入 confirm:true（请先 plan 核对）');
    }
    if (!ready()) throw new Error('ERP 页面未就绪');
    const runnable = (input.plans || []).filter(function (item) { return item && item.ok; });
    if (!runnable.length) throw new Error('没有可执行的试算明细（plans 中 ok=true 为空）');
    const delayMs = Number(input.delayMs);
    const wait = Number.isFinite(delayMs) && delayMs >= 0 ? delayMs : DEFAULT_WRITE_DELAY_MS;
    const onProgress = typeof input.onProgress === 'function' ? input.onProgress : null;
    const succeeded = [];
    const failed = [];
    for (let index = 0; index < runnable.length; index += 1) {
      const planItem = runnable[index];
      try {
        const item = await changeItem(planItem);
        succeeded.push(item);
        if (onProgress) {
          onProgress({
            index: index + 1,
            total: runnable.length,
            status: 'success',
            o_id: planItem.o_id,
          });
        }
      } catch (error) {
        const item = { o_id: planItem.o_id, error: String(error) };
        failed.push(item);
        if (onProgress) {
          onProgress({
            index: index + 1,
            total: runnable.length,
            status: 'failed',
            o_id: planItem.o_id,
            error: String(error),
          });
        }
      }
      if (index + 1 < runnable.length && wait > 0) await sleep(wait);
    }
    return {
      succeeded: succeeded,
      failed: failed,
      attempted: runnable.length,
      finishedAt: new Date().toISOString(),
    };
  }

  /**
   * Agent 任务格式兼容：直接吃 job.rules + job.targets / job.plan。
   */
  async function planJob(job) {
    return plan({ rules: job.rules, targets: job.targets });
  }

  async function executeJob(job, options) {
    options = options || {};
    const plans = (job.plan && job.plan.plans) || job.plans || options.plans || [];
    return execute({
      plans: plans,
      confirm: options.confirm === true,
      delayMs: options.delayMs,
      onProgress: options.onProgress,
    });
  }

  const api = {
    version: VERSION,
    ready: ready,
    loadOrder: loadOrder,
    planOrder: planOrder,
    plan: plan,
    execute: execute,
    changeItem: changeItem,
    planJob: planJob,
    executeJob: executeJob,
    searchSku: searchSku,
    acp: acp,
  };

  root.JstOrderExchange = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  return api;
}(typeof unsafeWindow !== 'undefined' ? unsafeWindow : (typeof globalThis !== 'undefined' ? globalThis : window)));
