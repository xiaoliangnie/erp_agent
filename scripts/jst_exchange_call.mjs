#!/usr/bin/env node
/**
 * 聚水潭订单换货 · Codex / 自动化调用辅助
 *
 * 核心逻辑在页面内跑（需要已登录的 list.aspx）。本脚本不直接连 ERP，只做三件事：
 *   1. 打印可注入的 core 源码路径与内容长度
 *   2. 生成 page.evaluate 片段（给 Codex / Playwright 粘贴）
 *   3. 可选：连接本机已开的 Chrome CDP，注入并调用 plan（需 --cdp）
 *
 * 示例：
 *   node scripts/jst_exchange_call.mjs print-inject
 *   node scripts/jst_exchange_call.mjs plan-snippet \
 *     --from OLD-SKU --to NEW-SKU --oids 10001,10002 \
 *     --source-style STYLE --target-style STYLE
 *   node scripts/jst_exchange_call.mjs plan \
 *     --from OLD-SKU --to NEW-SKU --oids 10001 \
 *     --source-style STYLE --target-style STYLE \
 *     --cdp http://127.0.0.1:9222
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const CORE_PATH = path.join(ROOT, 'frontend/js/jst-order-exchange.core.js');

function readCore() {
  return fs.readFileSync(CORE_PATH, 'utf8');
}

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token.startsWith('--')) {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (!next || next.startsWith('--')) {
        args[key] = true;
      } else {
        args[key] = next;
        i += 1;
      }
    } else {
      args._.push(token);
    }
  }
  return args;
}

function planInput(args) {
  const oids = String(args.oids || '')
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!args.from || !args.to || !oids.length) {
    throw new Error('plan 需要 --from --to --oids');
  }
  const input = {
    oIds: oids,
    from: String(args.from),
    to: String(args.to),
  };
  if (args['source-style']) input.sourceStyle = String(args['source-style']);
  if (args['target-style']) input.targetStyle = String(args['target-style']);
  if (args['exchange-type']) input.exchangeType = String(args['exchange-type']);
  return input;
}

function printHelp() {
  console.log(`用法:
  node scripts/jst_exchange_call.mjs print-inject
  node scripts/jst_exchange_call.mjs plan-snippet --from SKU --to SKU --oids 1,2 [--source-style S] [--target-style S] [--exchange-type same_style|special_mapping]
  node scripts/jst_exchange_call.mjs execute-snippet --plans-json '[...]'   # 需含 confirm
  node scripts/jst_exchange_call.mjs plan --from ... --to ... --oids ... --cdp http://127.0.0.1:9222

Codex 推荐流程:
  1. 浏览器打开并登录 聚水潭 /app/order/order/list.aspx
  2. page.evaluate(fs.readFileSync('frontend/js/jst-order-exchange.core.js','utf8'))
  3. page.evaluate(async (input) => JstOrderExchange.plan(input), input)
  4. 人工/Agent 核对 plans 后，confirm:true 再 execute
`);
}

const INJECT_SNIPPET = `async function injectJstOrderExchange(page, coreSource) {
  await page.evaluate((source) => {
    if (globalThis.JstOrderExchange && globalThis.JstOrderExchange.plan) return;
    const el = document.createElement('script');
    el.textContent = source + '\\n//# sourceURL=jst-order-exchange.core.js';
    (document.documentElement || document.head).appendChild(el);
    el.remove();
    if (!globalThis.JstOrderExchange) throw new Error('JstOrderExchange 未挂载');
  }, coreSource);
}
`;

function planSnippet(input) {
  return `// 先注入 core（只需一次）
const fs = await import('node:fs');
const core = fs.readFileSync('frontend/js/jst-order-exchange.core.js', 'utf8');
${INJECT_SNIPPET}
await injectJstOrderExchange(page, core);

const plan = await page.evaluate(async (input) => {
  if (!globalThis.JstOrderExchange) throw new Error('请先注入 jst-order-exchange.core.js');
  return JstOrderExchange.plan(input);
}, ${JSON.stringify(input, null, 2)});

console.log(plan);
// 核对 plan.plans 后：
// const result = await page.evaluate(async (plans) => {
//   return JstOrderExchange.execute({ plans, confirm: true });
// }, plan.plans.filter(p => p.ok));
`;
}

async function planViaCdp(input, cdpUrl) {
  let chromium;
  try {
    // 可选依赖：仓库未声明 playwright 时给出明确提示。
    ({ chromium } = await import('playwright'));
  } catch {
    throw new Error('未安装 playwright。请先 npm i -D playwright，或只用 plan-snippet 把代码交给 Codex 浏览器。');
  }
  const browser = await chromium.connectOverCDP(cdpUrl);
  try {
    const contexts = browser.contexts();
    const pages = contexts.flatMap((ctx) => ctx.pages());
    const page = pages.find((p) => /\/app\/order\/order\/list\.aspx/i.test(p.url())) || pages[0];
    if (!page) throw new Error('CDP 下没有可用页面，请先打开聚水潭订单列表');
    const core = readCore();
    await page.evaluate((source) => {
      if (globalThis.JstOrderExchange && globalThis.JstOrderExchange.plan) return;
      const el = document.createElement('script');
      el.textContent = source + '\n//# sourceURL=jst-order-exchange.core.js';
      (document.documentElement || document.head).appendChild(el);
      el.remove();
      if (!globalThis.JstOrderExchange) throw new Error('JstOrderExchange 未挂载');
    }, core);
    const plan = await page.evaluate(async (payload) => {
      if (!globalThis.JstOrderExchange) throw new Error('JstOrderExchange 未挂载');
      return JstOrderExchange.plan(payload);
    }, input);
    return { url: page.url(), plan };
  } finally {
    await browser.close();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const cmd = args._[0] || 'help';

  if (cmd === 'help' || args.help) {
    printHelp();
    return;
  }

  if (cmd === 'print-inject') {
    const core = readCore();
    console.log(JSON.stringify({
      path: CORE_PATH,
      bytes: Buffer.byteLength(core, 'utf8'),
      versionMatch: core.match(/VERSION = '([^']+)'/)?.[1] || null,
      expose: 'globalThis.JstOrderExchange',
      codex: [
        "const core = fs.readFileSync('frontend/js/jst-order-exchange.core.js','utf8');",
        "await page.evaluate((source) => { const el = document.createElement('script'); el.textContent = source; document.documentElement.appendChild(el); el.remove(); }, core);",
        "const plan = await page.evaluate(async (input) => JstOrderExchange.plan(input), { oIds:['...'], from:'A', to:'B', sourceStyle:'S', targetStyle:'S' });",
      ],
    }, null, 2));
    return;
  }

  if (cmd === 'plan-snippet') {
    process.stdout.write(planSnippet(planInput(args)));
    return;
  }

  if (cmd === 'execute-snippet') {
    const plansJson = args['plans-json'] || '[]';
    let plans;
    try { plans = JSON.parse(plansJson); }
    catch { throw new Error('--plans-json 必须是合法 JSON 数组'); }
    process.stdout.write(`const result = await page.evaluate(async (plans) => {
  if (!globalThis.JstOrderExchange) throw new Error('请先注入 jst-order-exchange.core.js');
  return JstOrderExchange.execute({ plans, confirm: true });
}, ${JSON.stringify(plans, null, 2)});
console.log(result);
`);
    return;
  }

  if (cmd === 'plan') {
    const input = planInput(args);
    if (!args.cdp) {
      process.stdout.write(planSnippet(input));
      console.error('\n# 未传 --cdp：已输出 snippet。真实执行请打开 ERP 页后由 Codex/Playwright 注入调用。');
      return;
    }
    const out = await planViaCdp(input, String(args.cdp));
    console.log(JSON.stringify(out, null, 2));
    return;
  }

  printHelp();
  process.exitCode = 1;
}

main().catch((error) => {
  console.error(String(error && error.stack || error));
  process.exitCode = 1;
});
