"""T07 临时探查：品牌与账号工作簿的 品牌与产品 / 说明 全量。用完即删。"""

from __future__ import annotations

import openpyxl


def main() -> None:
    wb = openpyxl.load_workbook(
        "../沁爽饮料-品牌与账号数据.xlsx", read_only=False, data_only=True
    )
    print("SHEETS:", wb.sheetnames)
    for title in ("品牌与产品", "说明"):
        if title not in wb.sheetnames:
            continue
        ws = wb[title]
        print("=" * 70)
        print("SRC SHEET:", title, "rows", ws.max_row, "cols", ws.max_column)
        for i, row in enumerate(ws.iter_rows(values_only=True), 1):
            print(i, [("" if v is None else str(v)) for v in row])
    wb.close()


if __name__ == "__main__":
    main()
