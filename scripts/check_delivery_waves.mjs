#!/usr/bin/env node
/**
 * 核对跟单三档催办口径：waves.ts 边界、ledger/model.ts 逐行回退、
 * tests/fixtures/delivery_waves.json。挂在 npm run build 前面，
 * 与 Python tests.test_delivery_reminders 共用同一份夹具。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixture = JSON.parse(
  fs.readFileSync(path.join(root, "tests/fixtures/delivery_waves.json"), "utf8"),
);
const wavesSource = fs.readFileSync(path.join(root, "frontend/src/pages/ledger/waves.ts"), "utf8");
const modelSource = fs.readFileSync(path.join(root, "frontend/src/pages/ledger/model.ts"), "utf8");

function waveOfDays(days) {
  if (days === null) return "none";
  if (days < 0) return "overdue";
  if (days <= 3) return "d3";
  if (days <= 10) return "d10";
  return "far";
}

function earliestDueDate(lines) {
  const pending = lines.filter((line) => line.qty - line.inQty > 0);
  const pool = pending.length ? pending : lines;
  let eta = "";
  let etaSource = "";
  const dates = new Set();
  for (const line of pool) {
    const due = line.deliveryDate || line.eta || "";
    if (!due) continue;
    dates.add(due);
    if (!eta || due < eta) {
      eta = due;
      etaSource = line.deliveryDate ? "交期" : "预计到货";
    }
  }
  return { eta, etaSource, etaSpread: dates.size };
}

const errors = [];

if (!/if \(days === null\) return "none"/.test(wavesSource)
    || !/if \(days < 0\) return "overdue"/.test(wavesSource)
    || !/if \(days <= 3\) return "d3"/.test(wavesSource)
    || !/if \(days <= 10\) return "d10"/.test(wavesSource)) {
  errors.push("waves.ts waveOfDays 边界与契约不一致");
}

if (!/deliveryDate \|\|/.test(modelSource) || !/function earliestDueDate/.test(modelSource)) {
  errors.push("ledger/model.ts 必须逐行 deliveryDate || eta 再取最早（earliestDueDate）");
}

for (const row of fixture.waveThresholds) {
  const actual = waveOfDays(row.days);
  if (actual !== row.wave) {
    errors.push(`waveOfDays(${row.days}) = ${actual}，夹具期望 ${row.wave}`);
  }
}

const mixed = fixture.mixedOrder;
const due = earliestDueDate(mixed.frontendLines);
if (due.eta !== mixed.expected.deliveryDate) {
  errors.push(`混合行整单交期 ${due.eta}，夹具期望 ${mixed.expected.deliveryDate}`);
}
if (due.etaSource !== mixed.expected.dateSource) {
  errors.push(`混合行来源 ${due.etaSource}，夹具期望 ${mixed.expected.dateSource}`);
}
if (due.etaSpread !== mixed.expected.etaSpread) {
  errors.push(`混合行交期个数 ${due.etaSpread}，夹具期望 ${mixed.expected.etaSpread}`);
}

const remaining = Math.round(
  (Date.parse(`${mixed.expected.deliveryDate}T00:00:00Z`)
    - Date.parse(`${fixture.today}T00:00:00Z`)) / 86400000,
);
const wave = waveOfDays(remaining);
if (wave !== mixed.expected.wave) {
  errors.push(`混合行波次 ${wave}（剩 ${remaining} 天），夹具期望 ${mixed.expected.wave}`);
}

if (errors.length) {
  console.error("跟单三档催办口径契约不一致：");
  for (const line of errors) console.error("  " + line);
  process.exit(1);
}
