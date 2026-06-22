#!/usr/bin/env python3
"""
make_t550_recipe.py — 產生 T550QVN10_TGT_G 的「baseline」配方（24 個 per-CCD 分區，含對位 M_AlignRoi）。

對齊 legacy Recipe 結構（見 recipes/DEFAULT/IP0/RecipeInfo.xml）：Recipe → M_AlignRoi + DetectRoiList + DetectIoiList。
影像：8160×5000 8-bit Mono8（T550QVN10 TFT 面板一角，含定位 fiducial + 週期像素陣列）。

⚠️ 這是 **baseline 起點**，非逐 CCD 調好的生產配方：
  - DetectRoi 用全幅(-1)；PitchX/Y 為起始值，**需在 Control Step1 用 FFT 量測按鈕逐 CCD 調**（每顆 CCD 看不同面板區域）。
  - M_AlignRoi 用 IP01 fiducial(亮圓) 座標作範例，AlignEnable=true；**golden 樣板與每顆 CCD 的 ReferX/Y 需在 Control Step1「設定對位 Mark」逐 CCD 擷取**（legacy model A：per-CCD 本地 ROI + 各自對位 Mark）。
    AlignEnable=true 但未提供 golden 時 IP 會優雅停用對位（不崩）。

用法：python3 scripts/make_t550_recipe.py [--out recipes] [--ccds 24]
"""
import argparse, os

RECIPE = "T550QVN10_TGT_G"

def detect_roi(pitch_x=33, pitch_y=17, dth=0.55, bth=1.55):
    return f"""    <DetectRoi>
      <StartX>-1</StartX><StartY>-1</StartY><EndX>-1</EndX><EndY>-1</EndY>
      <M_ImagePreproc>Ip_None</M_ImagePreproc>
      <SmoothTimes>1</SmoothTimes><SmoothTimes2>0</SmoothTimes2>
      <DarkThreshold>{dth}</DarkThreshold><BrightThreshold>{bth}</BrightThreshold>
      <SobelDetectEnable>false</SobelDetectEnable>
      <SobelSmoothTimes>1</SobelSmoothTimes><SobelSmoothTimes2>0</SobelSmoothTimes2>
      <SobelDarkThreshold>-20</SobelDarkThreshold><SobelBrightThreshold>20</SobelBrightThreshold>
      <AlgorithmWay>8-Way-Star</AlgorithmWay>
      <AlgorithmCompare>DIV</AlgorithmCompare>
      <M_AlgorithmWayCompare>Awc_None</M_AlgorithmWayCompare>
      <Adjustment />
      <PitchTime>3</PitchTime><ChooseAmount>-1</ChooseAmount>
      <PitchX>{pitch_x}</PitchX><PitchY>{pitch_y}</PitchY>
      <SearchX>1</SearchX><SearchY>1</SearchY>
      <EdgePassRatio>0.5</EdgePassRatio><EdgePassThreshold>32</EdgePassThreshold>
      <BlobMaxSize>100000</BlobMaxSize><BlobMinSize>80</BlobMinSize>
      <BlobElongation>100</BlobElongation><BlobFeretElong>100</BlobFeretElong>
      <BlobDarkMergeDistance>1</BlobDarkMergeDistance>
      <BlobBrightMergeDistance>1</BlobBrightMergeDistance>
      <BlobAllMergeDistance>1</BlobAllMergeDistance>
    </DetectRoi>"""

def recipe_xml():
    # M_AlignRoi：對位 Mark（baseline=IP01 亮圓 fiducial；per-CCD ReferX/Y + golden 在 Control 逐顆調）
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe>
  <M_AlignRoi>
    <AlignEnable>true</AlignEnable>
    <AlignResultSave>true</AlignResultSave>
    <PatternPath />
    <ReferX>600</ReferX>
    <ReferY>2160</ReferY>
    <SearchWidth>500</SearchWidth>
    <SearchHeight>500</SearchHeight>
  </M_AlignRoi>
  <DetectRoiList>
{detect_roi()}
  </DetectRoiList>
  <DetectIoiList />
</Recipe>
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="recipes")
    ap.add_argument("--ccds", type=int, default=24)
    a = ap.parse_args()
    xml = recipe_xml()
    for n in range(1, a.ccds + 1):
        part = f"IP{n:02d}"                      # 儲存鍵 = IpName（約束①：CCD 是 UI 名、IP0x 是儲存鍵）
        d = os.path.join(a.out, RECIPE, part)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "RecipeInfo.xml"), "w", encoding="utf-8") as f:
            f.write(xml)
    print(f"已產生 {a.ccds} 個 per-CCD 配方分區於 {a.out}/{RECIPE}/IP01..IP{a.ccds:02d}/RecipeInfo.xml")
    print("⚠️ baseline：PitchX/Y 與 per-CCD 對位 Mark(ReferX/Y+golden) 請在 Control Step1 逐 CCD 調校。")

if __name__ == "__main__":
    main()
