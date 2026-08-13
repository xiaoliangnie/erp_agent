/**
 * =============================================================================
 * 聚水潭订单 SKU 换货 · Codex 专用脚本（自包含，可直接注入 ERP 页）
 * =============================================================================
 *
 * 路径：/Users/yyyy/yyy/code/jst-order-exchange-codex.js
 * 版本：1.0.0
 *
 * 目标：让 Codex / 自动化代理「读懂本文件 → 注入 → 快速定位订单 → dry-run →
 *       （用户确认后）执行 ChangeItem」。不要模拟点击；走页面原生 _ACP。
 *
 * ---------------------------------------------------------------------------
 * 【Codex 必读操作手册】
 * ---------------------------------------------------------------------------
 *
 * 0. 前置
 *    - 用户浏览器已登录聚水潭
 *    - 当前标签页在订单列表：
 *        /app/order/order/list.aspx
 *      （常见完整入口：https://www.erp321.com/app/order/order/list.aspx）
 *    - 页面存在 window._ACP（官方异步调用）。没有就先等页面加载完或让用户打开正确菜单。
 *
 * 1. 注入（Playwright / Codex 浏览器，只需一次）
 *
 *    const fs = await import('node:fs');
 *    const source = fs.readFileSync('/Users/yyyy/yyy/code/jst-order-exchange-codex.js', 'utf8');
 *    await page.evaluate((code) => {
 *      const el = document.createElement('script');
 *      el.textContent = code;
 *      document.documentElement.appendChild(el);
 *      el.remove();
 *    }, source);
 *
 * 2. 快速定位订单（先找 o_id，再换货）
 *
 *    // 按内部单号
 *    await page.evaluate(async () => JstExchangeCodex.locate({ oIds: ['123456'] }));
 *
 *    // 按线上单号 / 平台单号
 *    await page.evaluate(async () => JstExchangeCodex.locate({ soIds: ['平台单号'] }));
 *
 *    // 按源 SKU 扫当前列表 + 逐单拉明细（最快的日常路径）
 *    await page.evaluate(async () => JstExchangeCodex.locate({
 *      containSku: '源SKU',
 *      limit: 50,
 *    }));
 *
 * 3. 试算（不写 ERP）
 *
 *    const dry = await page.evaluate(async () => JstExchangeCodex.plan({
 *      oIds: ['123456'],
 *      from: '源SKU',
 *      to: '目标SKU',
 *      sourceStyle: '款式编码',   // 同款换货必填
 *      targetStyle: '款式编码',
 *      // exchangeType: 'special_mapping', // 仅白名单跨款
 *    }));
 *    // 把 dry 摘要给用户看：exchangeable / skipped / 每单 reason
 *
 * 4. 一键「定位 + 试算」（推荐 Codex 默认路径，永不自动写）
 *
 *    const report = await page.evaluate(async () => JstExchangeCodex.quick({
 *      from: '源SKU',
 *      to: '目标SKU',
 *      containSku: '源SKU',       // 也可用 oIds / soIds
 *      sourceStyle: '款式',
 *      targetStyle: '款式',
 *      limit: 50,
 *    }));
 *
 * 5. 真实换货（必须用户明确说「确认执行」之后才调用）
 *
 *    await page.evaluate(async (plans) => JstExchangeCodex.execute({
 *      plans,
 *      confirm: true,            // 缺省会抛错，防止误写
 *    }), report.plan.plans.filter(p => p.ok));
 *
 * 6. 安全红线（Codex 必须遵守）
 *    - 默认只 plan / quick，绝不自动 execute
 *    - execute 必须 confirm:true，且仅使用刚刚 plan 返回且 ok===true 的明细
 *    - 跳过：取消/退款/关闭、找不到源 SKU、跨款（非白名单）
 *    - 单次 o_id 上限 500；写入间隔默认 250ms
 *    - 鞋垫特殊映射 XZ25401308-101 → XZ25401308-099* 时务必核对手寸
 *
 * 7. 诊断
 *    await page.evaluate(() => JstExchangeCodex.help());
 *    await page.evaluate(() => JstExchangeCodex.ready());
 *
 * ---------------------------------------------------------------------------
 * 暴露：globalThis.JstExchangeCodex
 * ---------------------------------------------------------------------------
 */
(function (root) {
  'use strict';

  var VERSION = '1.0.0';
  var DEFAULT_WRITE_DELAY_MS = 250;
  var DEFAULT_FORBIDDEN = '取消|退款|关闭|Cancelled|Delete|Merged';
  var MAX_OIDS = 500;

  /** 与采购系统 config/exchange-rules.json 对齐的内置白名单（Codex 离线可用） */
  var SPECIAL_MAPPINGS = [
    {
      name: 'XZ25401308-101 鞋垫规格映射',
      sourceSku: 'XZ25401308-101',
      sourceStyle: 'XZ25401308-101',
      targetStyle: 'XZ25401308-099',
      targetSkus: [
        'XZ25401308-09901', 'XZ25401308-09902', 'XZ25401308-09903', 'XZ25401308-09904',
        'XZ25401308-09905', 'XZ25401308-09906', 'XZ25401308-09907', 'XZ25401308-09908',
        'XZ25401308-09909', 'XZ25401308-09910', 'XZ25401308-09911', 'XZ25401308-09912',
        'XZ25401308099BL01', 'XZ25401308099BL02',
      ],
    },
  ];

  function w() { return root; }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function asList(raw) {
    if (raw == null || raw === '') return [];
    if (Array.isArray(raw)) return raw;
    return String(raw).split(/[\s,，;；]+/);
  }

  function uniqueStrings(raw) {
    var out = [];
    asList(raw).forEach(function (item) {
      var v = String(item || '').trim();
      if (v && out.indexOf(v) < 0) out.push(v);
    });
    return out;
  }

  function ready() {
    var win = w();
    var path = String(win.location && win.location.pathname || '');
    return typeof win._ACP === 'function' && /\/app\/order\/order\/list\.aspx/i.test(path);
  }

  function help() {
    return {
      version: VERSION,
      page: String(w().location && w().location.href || ''),
      ready: ready(),
      hasAcp: typeof w()._ACP === 'function',
      hasCallPage: typeof w()._CallPage === 'function',
      hasJTable: !!(w().jTable && w().jTable.Data),
      loadedOrderCount: listLoadedOrders().length,
      api: [
        'ready()',
        'help()',
        'listLoadedOrders()',
        'loadOrder(oId)',
        'locate({ oIds|soIds|containSku, limit })',
        'plan({ oIds, from, to, sourceStyle, targetStyle, exchangeType? })',
        'execute({ plans, confirm:true })',
        'quick({ from, to, oIds|soIds|containSku, sourceStyle, targetStyle, limit? })  // 只试算',
      ],
      codexRule: '先 locate/quick 展示 dry-run，用户确认后才 execute({confirm:true})',
    };
  }

  function acp(method) {
    var win = w();
    var args = Array.prototype.slice.call(arguments, 1);
    return new Promise(function (resolve, reject) {
      try {
        win._ACP.apply(win, [method, function (value) { resolve(value); }].concat(args));
      } catch (error) {
        reject(error);
      }
    });
  }

  function skuOf(item) {
    return String((item && (item.sku_id || item.skuId || item.sku)) || '').trim();
  }

  function styleOf(item) {
    return String((item && (item.i_id || item.iId || item.style_id || item.styleId)) || '').trim();
  }

  function qtyOf(item) {
    var value = Number(item && (item.qty != null ? item.qty : (item.qty_count != null ? item.qty_count : item.orderQty)));
    return Number.isFinite(value) ? value : 0;
  }

  function soOf(order) {
    return String((order && (order.so_id || order.outer_so_id || order.raw_so_id)) || '').trim();
  }

  function normalizeReload(value) {
    var source = value;
    if (Array.isArray(source)) source = source[0];
    if (typeof source === 'string') {
      try { source = JSON.parse(source); } catch (_) { return null; }
    }
    return source && (source.data || source);
  }

  /** 当前列表页已加载的订单行（不发请求，极快） */
  function listLoadedOrders() {
    var rows = w().jTable && w().jTable.Data && w().jTable.Data.datas;
    return Array.isArray(rows) ? rows.slice() : [];
  }

  /**
   * 按 o_id 拉完整订单（含 items）。优先列表缓存，再 _CallPage('ReloadOrdersV2')。
   */
  async function loadOrder(oid) {
    oid = String(oid || '').trim();
    if (!oid) throw new Error('o_id 不能为空');
    var order = listLoadedOrders().find(function (row) { return String(row.o_id) === oid; }) || null;
    var items = order && order.items;
    var win = w();
    if (typeof win._CallPage === 'function') {
      try {
        var reloaded = normalizeReload(win._CallPage('ReloadOrdersV2', oid, true));
        if (reloaded && String(reloaded.o_id || oid) === oid) {
          order = Object.assign({}, order || {}, reloaded);
          items = reloaded.items || items;
        }
      } catch (error) {
        if (!order) throw new Error('读取订单 ' + oid + ' 失败：' + String(error));
      }
    }
    if (!order) {
      return { o_id: oid, items: [], load_error: 'ERP 未返回该订单（可先在列表搜出该单再试）' };
    }
    return Object.assign({}, order, {
      o_id: oid,
      items: Array.isArray(items) ? items : [],
    });
  }

  function orderHasSku(order, sku) {
    sku = String(sku || '').trim();
    if (!sku) return false;
    if (Array.isArray(order.items) && order.items.some(function (it) { return skuOf(it) === sku; })) {
      return true;
    }
    // 列表摘要字段有时是 "SKU*qty;SKU2*qty" 或逗号拼接
    var blob = [order.skus, order.sku_id, order.sku_ids, order.i_id].map(function (x) {
      return String(x || '');
    }).join('|');
    return blob.indexOf(sku) >= 0;
  }

  function summarizeOrder(order, extra) {
    extra = extra || {};
    return Object.assign({
      o_id: String(order.o_id || ''),
      so_id: soOf(order),
      status: String(order.status || ''),
      shop_name: String(order.shop_name || ''),
      order_date: String(order.order_date || order.pay_date || ''),
      item_count: Array.isArray(order.items) ? order.items.length : 0,
      skus: Array.isArray(order.items)
        ? order.items.map(function (it) { return skuOf(it); }).filter(Boolean)
        : undefined,
    }, extra);
  }

  /**
   * 快速定位订单 → 明确 o_id 清单。
   *
   * @param {object} query
   * @param {string[]|string} [query.oIds]        内部订单号（最高优先）
   * @param {string[]|string} [query.soIds]       线上/平台单号
   * @param {string}          [query.containSku]  明细含此 SKU
   * @param {number}          [query.limit=50]
   * @param {boolean}         [query.reload=true] 命中后是否 ReloadOrdersV2 补明细
   */
  async function locate(query) {
    query = query || {};
    if (!ready()) {
      throw new Error('请先打开已登录的 /app/order/order/list.aspx（需要 _ACP）');
    }
    var limit = Number(query.limit);
    if (!Number.isFinite(limit) || limit < 1) limit = 50;
    if (limit > MAX_OIDS) limit = MAX_OIDS;
    var reload = query.reload !== false;

    var wantOids = uniqueStrings(query.oIds || query.o_ids);
    var wantSoIds = uniqueStrings(query.soIds || query.so_ids);
    var containSku = String(query.containSku || query.contain_sku || query.sku || '').trim();

    if (!wantOids.length && !wantSoIds.length && !containSku) {
      throw new Error('locate 至少提供 oIds / soIds / containSku 之一');
    }

    var loaded = listLoadedOrders();
    var candidates = [];
    var seen = {};

    function pushCandidate(row, match) {
      var oid = String(row.o_id || '').trim();
      if (!oid || seen[oid]) return;
      if (candidates.length >= limit) return;
      seen[oid] = true;
      candidates.push({ row: row, match: match });
    }

    // 1) 显式 o_id
    wantOids.forEach(function (oid) {
      var hit = loaded.find(function (row) { return String(row.o_id) === oid; });
      pushCandidate(hit || { o_id: oid }, 'o_id');
    });

    // 2) 线上单号：先扫当前页
    if (wantSoIds.length) {
      loaded.forEach(function (row) {
        var so = soOf(row);
        if (so && wantSoIds.indexOf(so) >= 0) pushCandidate(row, 'so_id');
      });
    }

    // 3) 含 SKU：先扫当前页摘要
    if (containSku) {
      loaded.forEach(function (row) {
        if (orderHasSku(row, containSku)) pushCandidate(row, 'contain_sku_loaded');
      });
    }

    // 4) 补拉明细，确认 so / sku（并给后续 plan 用）
    var orders = [];
    for (var i = 0; i < candidates.length; i += 1) {
      var c = candidates[i];
      var order = reload ? await loadOrder(c.row.o_id) : Object.assign({ items: [] }, c.row);
      // so_id 二次过滤（只靠摘要没命中时，用户给了 so 也可能要在已加载集外——此处无法全库搜）
      if (wantSoIds.length && c.match === 'so_id') {
        if (wantSoIds.indexOf(soOf(order)) < 0 && wantSoIds.indexOf(soOf(c.row)) < 0) continue;
      }
      if (containSku && c.match.indexOf('sku') >= 0) {
        if (!orderHasSku(order, containSku) && !orderHasSku(c.row, containSku)) continue;
      }
      // 用户只给 containSku：对已命中摘要的单再确认明细
      if (containSku && !wantOids.length && !wantSoIds.length) {
        if (!orderHasSku(order, containSku) && !orderHasSku(c.row, containSku)) continue;
      }
      orders.push(Object.assign(order, { _match: c.match }));
    }

    // 5) 仅 so_id 且当前页没有：尝试把每个 so 当关键词提示用户（不瞎点 UI）
    var missingSo = wantSoIds.filter(function (so) {
      return !orders.some(function (o) { return soOf(o) === so; });
    });

    // 6) 仅 o_id 列表：即使列表没有也 loadOrder 试一次
    if (wantOids.length) {
      for (var j = 0; j < wantOids.length; j += 1) {
        var oid2 = wantOids[j];
        if (orders.some(function (o) { return String(o.o_id) === oid2; })) continue;
        if (orders.length >= limit) break;
        orders.push(await loadOrder(oid2));
      }
    }

    // 7) containSku 且当前页命中太少：对当前页所有订单逐单 Reload 扫描（有上限）
    if (containSku && orders.length < limit) {
      var scanPool = loaded.slice(0, Math.min(loaded.length, limit));
      for (var k = 0; k < scanPool.length; k += 1) {
        var oid3 = String(scanPool[k].o_id || '');
        if (!oid3 || orders.some(function (o) { return String(o.o_id) === oid3; })) continue;
        if (orders.length >= limit) break;
        if (orderHasSku(scanPool[k], containSku)) {
          var full = reload ? await loadOrder(oid3) : scanPool[k];
          if (orderHasSku(full, containSku)) {
            orders.push(Object.assign(full, { _match: 'contain_sku_scan' }));
          }
        }
      }
    }

    var oIds = orders.map(function (o) { return String(o.o_id); }).filter(Boolean);
    return {
      total: orders.length,
      oIds: oIds,
      missingSoIds: missingSo,
      hint: missingSo.length
        ? '以下线上单号不在当前列表缓存：请在 ERP 搜索框查出后再 locate，或直接提供 o_id。' + missingSo.join(', ')
        : (containSku && !oIds.length
          ? '当前列表未找到含 SKU ' + containSku + ' 的订单。请先在 ERP 用商品编码筛选/搜索并加载结果页。'
          : ''),
      orders: orders.map(function (o) {
        return summarizeOrder(o, {
          match: o._match || (wantOids.length ? 'o_id' : ''),
          has_sku: containSku ? orderHasSku(o, containSku) : undefined,
          load_error: o.load_error || undefined,
        });
      }),
    };
  }

  function resolvePolicy(from, to, sourceStyle, targetStyle, exchangeType) {
    from = String(from || '').trim();
    to = String(to || '').trim();
    sourceStyle = String(sourceStyle || '').trim();
    targetStyle = String(targetStyle || '').trim();
    exchangeType = String(exchangeType || '').trim();

    var special = SPECIAL_MAPPINGS.find(function (m) { return m.sourceSku === from; });
    if (special) {
      if (special.targetSkus.indexOf(to) < 0) {
        throw new Error('商品 ' + from + ' 只能换成白名单目标之一（共 ' + special.targetSkus.length + ' 个）');
      }
      return {
        exchangeType: 'special_mapping',
        sourceStyle: special.sourceStyle,
        targetStyle: special.targetStyle,
        policyName: special.name,
      };
    }
    if (exchangeType === 'special_mapping') {
      throw new Error('源 SKU 不在特殊白名单，不能使用 special_mapping');
    }
    if (!sourceStyle || !targetStyle) {
      throw new Error('普通换货必须提供 sourceStyle 与 targetStyle（同款式）');
    }
    if (sourceStyle !== targetStyle) {
      throw new Error('普通商品只能同款换货：' + sourceStyle + ' ≠ ' + targetStyle);
    }
    return {
      exchangeType: 'same_style',
      sourceStyle: sourceStyle,
      targetStyle: targetStyle,
      policyName: '普通商品同款式换货',
    };
  }

  function buildRules(input) {
    var from = String(input.from || input.src_sku_id || '').trim();
    var to = String(input.to || input.new_sku_id || '').trim();
    if (!from || !to) throw new Error('from / to 不能为空');
    if (from === to) throw new Error('源 SKU 与目标 SKU 不能相同');
    var policy = resolvePolicy(
      from,
      to,
      input.sourceStyle || input.source_style,
      input.targetStyle || input.target_style,
      input.exchangeType || input.exchange_type
    );
    return {
      strategy: 'direct',
      forbidden_status_regex: String(input.forbiddenStatusRegex || DEFAULT_FORBIDDEN),
      replacements: [{
        from: from,
        to: to,
        sourceStyle: policy.sourceStyle,
        targetStyle: policy.targetStyle,
        exchangeType: policy.exchangeType,
        policyName: policy.policyName,
      }],
    };
  }

  function planOrder(order, rules) {
    var oid = String(order.o_id || '');
    if (order.load_error) return { o_id: oid, ok: false, reason: order.load_error };
    if (!Array.isArray(order.items) || !order.items.length) {
      return { o_id: oid, ok: false, reason: '订单没有可读取的商品明细' };
    }
    var forbidden = new RegExp(rules.forbidden_status_regex || DEFAULT_FORBIDDEN, 'i');
    var status = String(order.status || '');
    if (forbidden.test(status)) {
      return { o_id: oid, ok: false, reason: '状态不允许：' + status };
    }
    var replacement = rules.replacements[0];
    var source = String(replacement.from);
    var target = String(replacement.to);
    var lines = order.items.filter(function (item) { return skuOf(item) === source; });
    if (!lines.length) {
      return {
        o_id: oid,
        so_id: soOf(order),
        status: status,
        ok: false,
        reason: '未找到源 SKU（可能已经换过）',
        source_sku: source,
        target_sku: target,
      };
    }
    var sourceStyles = [];
    lines.forEach(function (item) {
      var st = styleOf(item);
      if (st && sourceStyles.indexOf(st) < 0) sourceStyles.push(st);
    });
    var sourceStyle = sourceStyles.length === 1
      ? sourceStyles[0]
      : String(replacement.sourceStyle || '');
    var targetStyle = String(replacement.targetStyle || '');
    var expectedType = String(replacement.exchangeType || '');
    var exchangeType = expectedType === 'special_mapping'
      ? 'special_mapping'
      : (sourceStyle && targetStyle && sourceStyle === targetStyle ? 'same_style' : 'unknown');
    if (expectedType === 'same_style' && exchangeType !== 'same_style') {
      return {
        o_id: oid,
        so_id: soOf(order),
        status: status,
        ok: false,
        reason: '订单中源/目标不是同一款式，已阻止',
        source_sku: source,
        target_sku: target,
        source_style: sourceStyle,
        target_style: targetStyle,
      };
    }
    return {
      o_id: oid,
      so_id: soOf(order),
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
      warning: exchangeType === 'special_mapping' ? '特殊白名单映射：请重点核对尺码' : '',
      source_lines: lines.map(function (item) {
        return { oi_id: item.oi_id || null, sku_id: source, qty: qtyOf(item) };
      }),
    };
  }

  /**
   * dry-run：按 o_id 清单试算，不写 ERP。
   */
  async function plan(input) {
    input = input || {};
    if (!ready()) throw new Error('ERP 订单列表页未就绪');
    var rules = input.rules || buildRules(input);
    var oids = uniqueStrings(input.oIds || input.o_ids || (input.targets && (input.targets.o_ids || input.targets.oIds)));
    if (!oids.length) throw new Error('plan 需要 oIds');
    if (oids.length > MAX_OIDS) throw new Error('单次最多 ' + MAX_OIDS + ' 个订单');

    var plans = [];
    for (var i = 0; i < oids.length; i += 1) {
      try {
        plans.push(planOrder(await loadOrder(oids[i]), rules));
      } catch (error) {
        plans.push({ o_id: String(oids[i]), ok: false, reason: String(error) });
      }
    }
    var exchangeable = plans.filter(function (p) { return p.ok; }).length;
    return {
      total: plans.length,
      exchangeable: exchangeable,
      skipped: plans.length - exchangeable,
      rules: {
        from: rules.replacements[0].from,
        to: rules.replacements[0].to,
        exchangeType: rules.replacements[0].exchangeType,
        policyName: rules.replacements[0].policyName,
      },
      plans: plans,
      summary: plans.map(function (p) {
        return p.ok
          ? ('✓ ' + p.o_id + ' ' + p.src_sku_id + '→' + p.new_sku_id + ' x' + p.qty)
          : ('✗ ' + p.o_id + ' ' + (p.reason || ''));
      }),
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
    if (planItem.mode !== 'ChangeItem') throw new Error('仅支持 ChangeItem');
    var rv = await acp(
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
   * 真实写入。confirm 必须为 true。
   */
  async function execute(input) {
    input = input || {};
    if (input.confirm !== true) {
      throw new Error('拒绝执行：必须 confirm:true，且仅在用户明确确认 dry-run 之后');
    }
    if (!ready()) throw new Error('ERP 页面未就绪');
    var runnable = (input.plans || []).filter(function (p) { return p && p.ok; });
    if (!runnable.length) throw new Error('没有 ok=true 的试算明细');
    var wait = Number(input.delayMs);
    if (!Number.isFinite(wait) || wait < 0) wait = DEFAULT_WRITE_DELAY_MS;

    var succeeded = [];
    var failed = [];
    for (var i = 0; i < runnable.length; i += 1) {
      try {
        succeeded.push(await changeItem(runnable[i]));
      } catch (error) {
        failed.push({ o_id: runnable[i].o_id, error: String(error) });
      }
      if (i + 1 < runnable.length && wait > 0) await sleep(wait);
    }
    return {
      succeeded: succeeded,
      failed: failed,
      attempted: runnable.length,
      finishedAt: new Date().toISOString(),
    };
  }

  /**
   * Codex 推荐入口：定位 + 试算，一次返回，绝不写入。
   *
   * quick({ from, to, oIds|soIds|containSku, sourceStyle, targetStyle, limit? })
   */
  async function quick(input) {
    input = input || {};
    var location = await locate({
      oIds: input.oIds || input.o_ids,
      soIds: input.soIds || input.so_ids,
      containSku: input.containSku || input.contain_sku || input.from,
      limit: input.limit,
      reload: true,
    });
    if (!location.oIds.length) {
      return {
        ok: false,
        stage: 'locate',
        location: location,
        plan: null,
        message: location.hint || '未定位到任何订单',
      };
    }
    var dry = await plan({
      oIds: location.oIds,
      from: input.from,
      to: input.to,
      sourceStyle: input.sourceStyle || input.source_style,
      targetStyle: input.targetStyle || input.target_style,
      exchangeType: input.exchangeType || input.exchange_type,
    });
    return {
      ok: dry.exchangeable > 0,
      stage: 'plan',
      location: location,
      plan: dry,
      message: '试算完成：可换 ' + dry.exchangeable + ' / 共 ' + dry.total
        + '。向用户展示 plan.summary 后，若确认再 execute。',
      next: dry.exchangeable > 0
        ? 'JstExchangeCodex.execute({ plans: plan.plans.filter(p=>p.ok), confirm: true })'
        : null,
    };
  }

  var api = {
    version: VERSION,
    SPECIAL_MAPPINGS: SPECIAL_MAPPINGS,
    ready: ready,
    help: help,
    listLoadedOrders: listLoadedOrders,
    loadOrder: loadOrder,
    locate: locate,
    plan: plan,
    planOrder: planOrder,
    execute: execute,
    changeItem: changeItem,
    quick: quick,
    acp: acp,
  };

  root.JstExchangeCodex = api;
  // 兼容旧名，方便与 Agent_demo 文档对照
  if (!root.JstOrderExchange) root.JstOrderExchange = api;

  if (typeof console !== 'undefined' && console.info) {
    console.info('[JstExchangeCodex] loaded', api.help());
  }
  return api;
}(typeof unsafeWindow !== 'undefined' ? unsafeWindow : (typeof globalThis !== 'undefined' ? globalThis : window)));
