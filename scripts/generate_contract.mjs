import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

// 明细 16 列 A–P：序号 / 图片 / 款式编码 / 商品编码 / 国标码（条码）/ 执行标准 /
// 品名 / 分类 / 虚拟分类 / 材质工艺 / 包装方式 / 数量 / 单位 / 单价 / 小计 / 备注。
// 数量 L、单价 N、小计 O = N*L。国标码仍是 EAN，不要和执行标准（GB/T…）混用。

const artifactToolPath = String(process.env.CONTRACT_ARTIFACT_TOOL_PATH || "").trim();
const artifactToolSpecifier = artifactToolPath
  ? pathToFileURL(path.resolve(artifactToolPath)).href
  : "@oai/artifact-tool";
let SpreadsheetFile;
let Workbook;
try {
  ({ SpreadsheetFile, Workbook } = await import(artifactToolSpecifier));
} catch (error) {
  throw new Error(
    "无法加载 @oai/artifact-tool。请在 .env 配置 CONTRACT_ARTIFACT_TOOL_PATH，" +
    `当前加载位置：${artifactToolSpecifier}；原始错误：${error instanceof Error ? error.message : String(error)}`,
  );
}

const [inputPath, outputPath, previewPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error("用法：node scripts/generate_contract.mjs input.json output.xlsx [preview.png]");
}

const model = JSON.parse(await fs.readFile(inputPath, "utf8"));
if (!Array.isArray(model.items) || model.items.length === 0) {
  throw new Error("采购合同至少需要一条商品明细");
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("合同");
sheet.showGridLines = false;

const itemStart = 9;
const itemEnd = itemStart + model.items.length - 1;
const totalRow = itemEnd + 1;
const packagingRow = totalRow + 1;
const paymentRow = totalRow + 2;
const inspectionRow = totalRow + 3;
const addressRow = totalRow + 4;
const termsRow = totalRow + 5;
const partiesRow = totalRow + 6;
const signaturesRow = totalRow + 7;
const finalRow = signaturesRow;

const columnWidths = [13.34, 17, 17, 17, 16.58, 16.5, 19.64, 8.4, 9.06, 15.2, 16.93, 12.34, 12.34, 12.7, 12.34, 8.68];
for (let col = 0; col < columnWidths.length; col += 1) {
  sheet.getRangeByIndexes(0, col, finalRow, 1).format.columnWidth = columnWidths[col];
}

const merges = [
  "A1:P2", "A3:B3", "C3:P3",
  "B4:H4", "J4:P4", "B5:H5", "J5:P5", "B6:H6", "J6:P6", "B7:H7", "J7:P7",
  `A${totalRow}:N${totalRow}`,
  `B${packagingRow}:P${packagingRow}`,
  `B${paymentRow}:P${paymentRow}`,
  `B${inspectionRow}:P${inspectionRow}`,
  `B${addressRow}:P${addressRow}`,
  `A${termsRow}:P${termsRow}`,
  `B${partiesRow}:I${partiesRow}`, `J${partiesRow}:K${partiesRow}`, `M${partiesRow}:P${partiesRow}`,
  `B${signaturesRow}:C${signaturesRow}`, `F${signaturesRow}:H${signaturesRow}`, `J${signaturesRow}:P${signaturesRow}`,
];
for (const address of merges) sheet.getRange(address).merge();

const all = sheet.getRange(`A1:P${finalRow}`);
all.format = {
  font: { typeface: "Microsoft YaHei", fontSize: 10, color: "#000000" },
  verticalAlignment: "center",
  wrapText: true,
  borders: { preset: "all", style: "thin", color: "#000000" },
};

sheet.getRange("A1").values = [["杭 州 无 际 云 帆 采 购 单"]];
sheet.getRange("A1:P2").format = {
  font: { typeface: "Microsoft YaHei", fontSize: 16 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
sheet.getRange("A3").values = [["项目负责人/设计师"]];
sheet.getRange("C3").values = [[model.projectLead || ""]];

sheet.getRange("A4").values = [["下单日期"]];
sheet.getRange("B4").values = [[new Date(`${model.orderDate}T00:00:00`)]];
sheet.getRange("I4").values = [["交货日期"]];
sheet.getRange("J4").values = [[model.deliveryDate ? new Date(`${model.deliveryDate}T00:00:00`) : null]];
sheet.getRange("B4:H4").format.numberFormat = 'yyyy"年"m"月"d"日"';
sheet.getRange("J4:P4").format.numberFormat = 'yyyy"年"m"月"d"日"';

sheet.getRange("A5").values = [["需方："]];
sheet.getRange("B5").values = [[model.buyer.company_name]];
sheet.getRange("I5").values = [["供方："]];
sheet.getRange("J5").values = [[model.supplier.legalName]];
sheet.getRange("A6").values = [["送货地址："]];
sheet.getRange("B6").values = [[model.buyer.warehouse_name || ""]];
sheet.getRange("I6").values = [["地址："]];
sheet.getRange("J6").values = [[model.supplier.address]];
sheet.getRange("A7").values = [["联系人："]];
sheet.getRange("B7").values = [[model.buyer.contact || model.deliveryAddress]];
sheet.getRange("I7").values = [["联系人："]];
sheet.getRange("J7").values = [[model.supplier.contact]];

const rateText = Number(model.invoice.taxRate).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const unitPriceHeader = model.invoice.type === "no_invoice"
  ? "单价（不开票）"
  : `单价（含${rateText}%${model.invoice.label}税）`;
sheet.getRange("A8:P8").values = [[
  "序号", "图片", "款式编码", "商品编码", "国标码", "执行标准", "品名", "分类", "虚拟分类",
  "材质工艺", "包装方式", "数量", "单位", unitPriceHeader, "小计：元", "备注",
]];
sheet.getRange("A8:P8").format = {
  font: { typeface: "Microsoft YaHei", fontSize: 10 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
};

const itemValues = model.items.map((item, index) => [
  index + 1, "", item.styleCode, item.sku, null, item.gbStandard || "", item.name, item.category,
  item.virtualCategory, item.materialProcess, item.packaging, item.quantity, item.unit,
  item.unitPrice, null, item.remark,
]);
sheet.getRange(`C${itemStart}:G${itemEnd}`).format.numberFormat = "@";
sheet.getRange(`A${itemStart}:P${itemEnd}`).values = itemValues;
sheet.getRange(`E${itemStart}:E${itemEnd}`).formulas = model.items.map(item => [
  /^\d+$/.test(String(item.nationalCode || ""))
    ? `=TEXT(${item.nationalCode},"0")`
    : `="${String(item.nationalCode || "").replaceAll('"', '""')}"`,
]);
sheet.getRange(`O${itemStart}`).formulas = [[`=N${itemStart}*L${itemStart}`]];
if (itemEnd > itemStart) sheet.getRange(`O${itemStart}:O${itemEnd}`).fillDown();
sheet.getRange(`A${itemStart}:P${itemEnd}`).format = {
  font: { typeface: "Microsoft YaHei", fontSize: 9 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
};
sheet.getRange(`L${itemStart}:L${itemEnd}`).format.numberFormat = "#,##0";
sheet.getRange(`N${itemStart}:O${itemEnd}`).format.numberFormat = '"￥"#,##0.00';

for (let index = 0; index < model.items.length; index += 1) {
  const row = itemStart + index;
  const imagePath = model.items[index].imagePath;
  sheet.getRange(`A${row}:P${row}`).format.rowHeight = 82;
  if (!imagePath) {
    sheet.getRange(`B${row}`).values = [["暂无图片"]];
    continue;
  }
  try {
    const bytes = await fs.readFile(imagePath);
    const extension = path.extname(imagePath).toLowerCase();
    const mimeType = extension === ".jpg" || extension === ".jpeg"
      ? "image/jpeg"
      : extension === ".webp" ? "image/webp" : "image/png";
    const dataUrl = `data:${mimeType};base64,${bytes.toString("base64")}`;
    sheet.images.add({
      dataUrl,
      anchor: { from: { row: row - 1, col: 1, rowOffsetPx: 3, colOffsetPx: 3 }, extent: { widthPx: 86, heightPx: 76 } },
    });
  } catch {
    sheet.getRange(`B${row}`).values = [["图片读取失败"]];
  }
}

sheet.getRange(`A${totalRow}`).values = [["合计金额："]];
sheet.getRange(`O${totalRow}`).formulas = [[`=SUM(O${itemStart}:O${itemEnd})`]];
sheet.getRange(`A${totalRow}:N${totalRow}`).format.horizontalAlignment = "right";
sheet.getRange(`O${totalRow}`).format.numberFormat = '"￥"#,##0.00';

sheet.getRange(`A${packagingRow}`).values = [["包装"]];
sheet.getRange(`B${packagingRow}`).values = [[model.packagingTerms]];
sheet.getRange(`A${paymentRow}`).values = [["付款方式"]];
sheet.getRange(`B${paymentRow}`).values = [[model.paymentTerms]];
sheet.getRange(`A${inspectionRow}`).values = [["检验标准"]];
sheet.getRange(`B${inspectionRow}`).values = [[model.inspectionStandards]];
sheet.getRange(`A${addressRow}`).values = [["送货地址"]];
sheet.getRange(`B${addressRow}`).values = [[model.deliveryAddress]];
for (const row of [packagingRow, paymentRow, inspectionRow, addressRow]) {
  sheet.getRange(`A${row}`).format.horizontalAlignment = "center";
  sheet.getRange(`B${row}:P${row}`).format.horizontalAlignment = "left";
  sheet.getRange(`A${row}:P${row}`).format.rowHeight = 46;
}

sheet.getRange(`A${termsRow}`).values = [[model.terms.join("\n")]];
sheet.getRange(`A${termsRow}:P${termsRow}`).format = {
  font: { typeface: "Microsoft YaHei", fontSize: 9 },
  horizontalAlignment: "left",
  verticalAlignment: "top",
  wrapText: true,
  rowHeight: 270,
};

sheet.getRange(`A${partiesRow}`).values = [["需方："]];
sheet.getRange(`B${partiesRow}`).values = [[model.buyer.company_name]];
sheet.getRange(`J${partiesRow}`).values = [["供方："]];
sheet.getRange(`L${partiesRow}`).values = [[model.supplier.legalName]];
sheet.getRange(`A${signaturesRow}`).values = [["申请人："]];
sheet.getRange(`B${signaturesRow}`).values = [[model.applicant]];
sheet.getRange(`D${signaturesRow}`).values = [["直属领导签字："]];
sheet.getRange(`I${signaturesRow}`).values = [["副总经理签字："]];
sheet.getRange(`A${signaturesRow}:P${signaturesRow}`).format = {
  font: { typeface: "SimSun", fontSize: 11, bold: true },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};

for (const row of [1, 2]) sheet.getRange(`A${row}:P${row}`).format.rowHeight = 18;
sheet.getRange("A3:P7").format.rowHeight = 23;
sheet.getRange("A8:P8").format.rowHeight = 34;
sheet.getRange(`A${totalRow}:P${totalRow}`).format.rowHeight = 23;
sheet.getRange(`A${partiesRow}:P${signaturesRow}`).format.rowHeight = 24;

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

if (previewPath) {
  await fs.mkdir(path.dirname(previewPath), { recursive: true });
  const preview = await workbook.render({ sheetName: "合同", autoCrop: "all", scale: 1.5, format: "png" });
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
}
