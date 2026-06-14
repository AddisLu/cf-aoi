#!/usr/bin/env python3
"""
CF-AOI 跨架構結果一致性比對（x86 RTX2080S sm_75  vs  ARM Spark GB10 sm_121）。

比對兩份 IP 輸出的 {panel}_{recipe}_ResultInfo.json：
  Tier-0（整數/字串幾何）：要求**完全一致**（跨架整數無不一致理由）。
    DefectCnt / num_defects / GC_X / GC_Y / Size / Width / Height /
    X_Min/X_Max/Y_Min/Y_Max / GlobalPosX/GlobalPosY / Type / RunIndex / RoiIndex / roi_offset_*
  Tier-1（浮點，主要是 GL_Mean）：容差 |Δ| ≤ --glmean-tol（預設 0.5）。
    **無論是否超標都報告實測 max|Δ|** —— 這是關鍵診斷數字：
    跨架浮點差異通常是 ULP 級（≪0.5）；若實測接近容差上限，反而要警覺別被寬容差掩蓋問題。
  忽略：total_time_ms / process_time_ms（計時，本來就非決定性）。

用法：  compare_results.py  A.json  B.json  [--glmean-tol 0.5] [--max-report 20]
退出碼：0 = PASS，1 = FAIL（供自動化用 $?）。
純標準庫，無第三方相依。
"""
import argparse
import json
import sys

# 浮點欄位（容差比對）；其餘 int/string 一律 Tier-0 完全一致
FLOAT_FIELDS = {"GL_Mean", "GL_Sigma", "CV_Sigma", "CV_Mean", "AiScore", "MeanValue"}
# 比對時忽略的計時欄位
IGNORE_TOP = {"total_time_ms"}
IGNORE_ROI = {"process_time_ms"}
# 頂層要比對的純量
TOP_SCALARS = ("DefectCnt", "pass", "image_width", "image_height",
               "recipe_name", "panel_id", "AiOkCnt", "RuleOkCnt")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def flatten_defects(j):
    """攤平成 [(roi_index, defect_dict), ...]，順序＝IP 的 canonical 排序。"""
    out = []
    for roi in j.get("RoiInfoList", []):
        ridx = roi.get("RoiIndex")
        for d in roi.get("DefectInfoList", []):
            out.append((ridx, d))
    return out


def defect_key(d):
    """count 不同時用來對齊/列孤立缺陷（全域座標 + 型別）。"""
    return (d.get("GlobalPosX"), d.get("GlobalPosY"), d.get("Type"))


def main():
    ap = argparse.ArgumentParser(description="CF-AOI 跨架構結果一致性比對")
    ap.add_argument("a", help="第一份 ResultInfo.json（例：x86）")
    ap.add_argument("b", help="第二份 ResultInfo.json（例：ARM）")
    ap.add_argument("--glmean-tol", type=float, default=0.5, help="GL_Mean 等浮點容差（預設 0.5）")
    ap.add_argument("--max-report", type=int, default=20, help="最多列出幾筆不符（預設 20）")
    args = ap.parse_args()

    A, B = load(args.a), load(args.b)
    da, db = flatten_defects(A), flatten_defects(B)
    print(f"[compare] A={args.a}  缺陷={len(da)}")
    print(f"[compare] B={args.b}  缺陷={len(db)}")

    tier0_fail = []       # 頂層 + 整數/字串不一致
    tier1_over = []       # 浮點超容差
    max_delta = 0.0       # 實測 GL_Mean 最大 |Δ|（關鍵診斷）
    max_delta_where = None

    # ---- 頂層純量 ----
    for k in TOP_SCALARS:
        if A.get(k) != B.get(k):
            tier0_fail.append(f"top.{k}: {A.get(k)!r} != {B.get(k)!r}")

    # ---- ROI 層 ----
    ra, rb = A.get("RoiInfoList", []), B.get("RoiInfoList", [])
    if len(ra) != len(rb):
        tier0_fail.append(f"RoiInfoList 長度: {len(ra)} != {len(rb)}")
    else:
        for i, (x, y) in enumerate(zip(ra, rb)):
            for k in ("RoiIndex", "roi_offset_x", "roi_offset_y", "num_defects"):
                if x.get(k) != y.get(k):
                    tier0_fail.append(f"ROI[{i}].{k}: {x.get(k)} != {y.get(k)}")

    # ---- 缺陷層 ----
    if len(da) != len(db):
        # 數量不同 → 用全域座標對齊，列孤立缺陷（多半是閾值邊界 FP 翻面）
        ka = {defect_key(d) for _, d in da}
        kb = {defect_key(d) for _, d in db}
        only_a, only_b = ka - kb, kb - ka
        tier0_fail.append(f"缺陷數不同: A={len(da)} B={len(db)} "
                          f"（僅A {len(only_a)} 筆 / 僅B {len(only_b)} 筆）")
        for tag, s in (("僅在A", only_a), ("僅在B", only_b)):
            for kk in list(s)[:args.max_report]:
                print(f"  [{tag}] GlobalPos=({kk[0]},{kk[1]}) Type={kk[2]}")
    else:
        # 數量相同 → 同序逐筆比（兩端 canonical 排序一致）
        for idx, ((_, a), (_, b)) in enumerate(zip(da, db)):
            for key in set(a) | set(b):
                if key in FLOAT_FIELDS:
                    va, vb = float(a.get(key, 0)), float(b.get(key, 0))
                    delta = abs(va - vb)
                    if key == "GL_Mean" and delta > max_delta:
                        max_delta = delta
                        max_delta_where = (idx, a.get("GC_X"), a.get("GC_Y"))
                    if delta > args.glmean_tol:
                        tier1_over.append(f"  #{idx} {key} |Δ|={delta:.5f} "
                                          f"({va} vs {vb}) @GC=({a.get('GC_X')},{a.get('GC_Y')})")
                else:
                    if a.get(key) != b.get(key):
                        tier0_fail.append(f"  #{idx} TIER0 {key}: {a.get(key)} != {b.get(key)} "
                                          f"@GC=({a.get('GC_X')},{a.get('GC_Y')})")

    # ---- 總結 ----
    print("-" * 60)
    print(f"[Tier-1] GL_Mean 實測 max|Δ| = {max_delta:.6f}"
          + (f"  @#{max_delta_where[0]} GC=({max_delta_where[1]},{max_delta_where[2]})"
             if max_delta_where else "")
          + f"  (容差 {args.glmean_tol})")
    if max_delta > 0 and max_delta <= args.glmean_tol:
        print(f"  ⚠ max|Δ|>0：跨架浮點有差異（{max_delta:.6f}），仍在容差內；若接近上限要警覺。")

    if tier0_fail:
        print(f"[Tier-0] ✗ {len(tier0_fail)} 項整數/結構不一致（應為 0）：")
        for line in tier0_fail[:args.max_report]:
            print("  " + line if not line.startswith("  ") else line)
        if len(tier0_fail) > args.max_report:
            print(f"  …還有 {len(tier0_fail) - args.max_report} 項")
    if tier1_over:
        print(f"[Tier-1] ✗ {len(tier1_over)} 筆浮點超容差 {args.glmean_tol}：")
        for line in tier1_over[:args.max_report]:
            print(line)

    ok = (not tier0_fail) and (not tier1_over)
    print("=" * 60)
    print("✅ PASS：跨架結果一致（Tier-0 全等，GL_Mean 在容差內）" if ok
          else "❌ FAIL：見上方不一致項")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
