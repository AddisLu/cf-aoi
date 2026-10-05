#!/usr/bin/env python3
"""
recipe_writer — 自動調參結果 → legacy RecipeInfo.xml（每台 CCD 一份分區）。

- 檢測區（晶片）→ DetectRoi，**panel 座標**（IP 依 sliceIndex 平移，I8）；EndX/EndY 為「不含」端點
  （IP zone_rect：寬 = End − Start）。
- dummy 帶 / 外圍 → DetectIoi（IP 只裁圖存檔、不檢測，日後給 AI）。
- 演算法：DIV（`AlgorithmCompare=DIV` + `Awc_None` → IP mode 0，三域守門已驗）；SUB 另寫。
- PitchX/Y 取整數（kernel 吃整數），SearchX/SearchY = local search（SearchY → fast_search_range）。
- Blob 過濾預設關（0），不靠過濾壓誤判——誤判要靠門檻與檢測區本身解決。
"""
from xml.sax.saxutils import escape


def detect_roi(r, pitch, th, search=(1, 1), mode='div'):
    px, py = int(round(pitch[0])), int(round(pitch[1]))
    comp = 'DIV' if mode == 'div' else 'SUB'
    awc = 'Awc_None' if mode == 'div' else 'Awc_8_Way_Star_Sub'
    return f"""    <DetectRoi>
      <StartX>{r['x0']}</StartX>
      <StartY>{r['y0']}</StartY>
      <EndX>{r['x1']}</EndX>
      <EndY>{r['y1']}</EndY>
      <M_ImagePreproc>Ip_None</M_ImagePreproc>
      <SmoothTimes>1</SmoothTimes>
      <SmoothTimes2>0</SmoothTimes2>
      <DarkThreshold>{th['dark']}</DarkThreshold>
      <BrightThreshold>{th['bright']}</BrightThreshold>
      <SobelDetectEnable>false</SobelDetectEnable>
      <SobelSmoothTimes>1</SobelSmoothTimes>
      <SobelSmoothTimes2>0</SobelSmoothTimes2>
      <SobelDarkThreshold>-20</SobelDarkThreshold>
      <SobelBrightThreshold>20</SobelBrightThreshold>
      <AlgorithmWay>8-Way-Star</AlgorithmWay>
      <AlgorithmCompare>{comp}</AlgorithmCompare>
      <M_AlgorithmWayCompare>{awc}</M_AlgorithmWayCompare>
      <Adjustment />
      <PitchTime>3</PitchTime>
      <ChooseAmount>-1</ChooseAmount>
      <PitchX>{px}</PitchX>
      <PitchY>{py}</PitchY>
      <SearchX>{search[0]}</SearchX>
      <SearchY>{search[1]}</SearchY>
      <EdgePassRatio>0.5</EdgePassRatio>
      <EdgePassThreshold>32</EdgePassThreshold>
      <BlobMaxSize>0</BlobMaxSize>
      <BlobMinSize>0</BlobMinSize>
      <BlobElongation>100</BlobElongation>
      <BlobFeretElong>100</BlobFeretElong>
      <BlobDarkMergeDistance>0</BlobDarkMergeDistance>
      <BlobBrightMergeDistance>0</BlobBrightMergeDistance>
      <BlobAllMergeDistance>0</BlobAllMergeDistance>
    </DetectRoi>
"""


def detect_ioi(r, name=''):
    return f"""    <DetectIoi>
      <StartX>{r['x0']}</StartX>
      <StartY>{r['y0']}</StartY>
      <EndX>{r['x1']}</EndX>
      <EndY>{r['y1']}</EndY>
    </DetectIoi>
"""


def make_recipe(chips, ioi, pitch, th, search=(1, 1), mode='div'):
    """th：單一 dict（全部晶片同門檻）或 list（每顆晶片一組，混切用）。"""
    ths = th if isinstance(th, list) else [th] * len(chips)
    rois = ''.join(detect_roi(r, pitch, t, search, mode) for r, t in zip(chips, ths))
    iois = ''.join(detect_ioi(r, f'dummy{i:02d}') for i, r in enumerate(ioi))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Recipe>
  <M_AlignRoi>
    <AlignEnable>false</AlignEnable>
    <AlignResultSave>true</AlignResultSave>
    <PatternPath />
    <ReferX>-1</ReferX>
    <ReferY>-1</ReferY>
    <SearchWidth>1000</SearchWidth>
    <SearchHeight>1000</SearchHeight>
  </M_AlignRoi>
  <DetectRoiList>
{rois}  </DetectRoiList>
  <DetectIoiList>
{iois}  </DetectIoiList>
</Recipe>
"""
