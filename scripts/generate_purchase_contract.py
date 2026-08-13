# -*- coding: utf-8 -*-
"""命令行生成一张采购合同。"""
import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.contracts import generate_contract  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="从聚水潭实时采购单生成 Excel 合同")
    parser.add_argument("--po-id", required=True, help="采购单号")
    parser.add_argument(
        "--invoice-type", required=True,
        choices=["no_invoice", "normal_invoice", "special_invoice"],
        help="不开票 / 普票 / 专票",
    )
    parser.add_argument("--tax-rate", type=float, help="覆盖供应商字典中的税率")
    parser.add_argument("--output", help="输出 xlsx 路径")
    parser.add_argument("--preview", help="可选的预览 PNG 路径")
    args = parser.parse_args()
    output = args.output or str(ROOT / "outputs" / f"采购合同-{args.po_id}-{args.invoice_type}.xlsx")
    path = generate_contract(
        args.po_id, args.invoice_type, output,
        tax_rate=args.tax_rate, preview_path=args.preview,
    )
    print(path)


if __name__ == "__main__":
    main()
