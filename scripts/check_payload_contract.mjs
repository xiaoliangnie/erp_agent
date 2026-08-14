#!/usr/bin/env node
/**
 * 核对 frontend/src/data/payload.ts 的有序列名与 tests/fixtures/payload_contract.json。
 * 挂在 npm run build 前面，避免 Python / TypeScript 两边列顺序悄悄错位。
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = fs.readFileSync(path.join(root, "frontend/src/data/payload.ts"), "utf8");
const fixture = JSON.parse(
  fs.readFileSync(path.join(root, "tests/fixtures/payload_contract.json"), "utf8"),
);

function readColumns(name) {
  const match = source.match(new RegExp(`export const ${name} = \\[([\\s\\S]*?)\\] as const;`));
  if (!match) {
    throw new Error(`payload.ts 里找不到 export const ${name} = […] as const`);
  }
  return [...match[1].matchAll(/"([^"]+)"/g)].map((item) => item[1]);
}

const actual = {
  dashboard: {
    orderColumns: readColumns("DASHBOARD_ORDER_COLUMNS"),
    lineColumns: readColumns("DASHBOARD_LINE_COLUMNS"),
  },
  delivery: {
    orderColumns: readColumns("DELIVERY_ORDER_COLUMNS"),
    lineColumns: readColumns("DELIVERY_LINE_COLUMNS"),
  },
};

const mismatches = [];
for (const page of ["dashboard", "delivery"]) {
  for (const key of ["orderColumns", "lineColumns"]) {
    const left = actual[page][key];
    const right = fixture[page][key];
    if (JSON.stringify(left) !== JSON.stringify(right)) {
      mismatches.push(`${page}.${key}: payload.ts=${JSON.stringify(left)} fixture=${JSON.stringify(right)}`);
    }
    const widthKey = key === "orderColumns" ? "orderWidth" : "lineWidth";
    if (right.length !== fixture[page][widthKey]) {
      mismatches.push(`${page}.${widthKey}: fixture 宽度 ${fixture[page][widthKey]} ≠ 列名 ${right.length}`);
    }
  }
}

if (mismatches.length) {
  console.error("payload 列名契约不一致：");
  for (const line of mismatches) console.error("  " + line);
  process.exit(1);
}
