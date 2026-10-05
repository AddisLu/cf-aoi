#!/usr/bin/env python3
"""
recipe_writer — 自動調參結果 → legacy RecipeInfo.xml（每台 CCD 一份分區）。

- 檢測區（晶片）→ DetectRoi，**panel 座標**（IP 依 sliceIndex 平移，I8）；EndX/EndY 為「不含」端點
  （IP zone_rect：寬 = End − Start）。
- dummy 帶 / 外圍 → DetectIoi（IP 只裁圖存檔、不檢測，日後給 AI）。
- 演算法：div（mode 0）/ sub（mode 1，T550 生產參數）/ divvote（mode 2 + 多尺度），見 METHODS。
- PitchX/Y 取整數（kernel 吃整數），SearchX/SearchY = local search（SearchY → fast_search_range）。
- Blob 過濾預設關（0），不靠過濾壓誤判——誤判要靠門檻與檢測區本身解決。
"""
from xml.sax.saxutils import escape


# 偵測方案（IP 三域守門：M_AlgorithmWayCompare 權威，見 docs/CLAUDE.md §5）
#   div      mode 0：比例單次（8-Way，center / mean8），門檻為比值（0.6 / 1.4）
#   sub      mode 1：legacy SUB 灰階差逐路投票（PitchTime×8 路、ChooseAmount 路超標才算），門檻為灰階差（−16 / +17）
#   divvote  mode 2：DIV 逐路投票融合（比值域）＋ 多尺度（大顆缺陷補強）
METHODS = {
    'div':     {'compare': 'DIV', 'awc': 'Awc_None', 'preproc': 'Ip_None', 'smooth': 0, 'smooth2': 0,
                'pitch_time': 3, 'choose': -1, 'multiscale': 0},
    'sub':     {'compare': 'SUB', 'awc': 'Awc_8_Way_Star_Sub', 'preproc': 'Ip_Remap', 'smooth': 0, 'smooth2': 1,
                'pitch_time': 2, 'choose': 13, 'multiscale': 0},          # = T550 生產配方
    'divvote': {'compare': 'DIV', 'awc': 'Awc_8_Way_Star_Div', 'preproc': 'Ip_None', 'smooth': 0, 'smooth2': 0,
                'pitch_time': 2, 'choose': 13, 'multiscale': 1},
}


def detect_roi(r, pitch, th, search=(1, 1), mode='div', opt=None):
    px, py = int(round(pitch[0])), int(round(pitch[1]))
    m = dict(METHODS[mode], **(opt or {}))
    return f"""    <DetectRoi>
      <StartX>{r['x0']}</StartX>
      <StartY>{r['y0']}</StartY>
      <EndX>{r['x1']}</EndX>
      <EndY>{r['y1']}</EndY>
      <M_ImagePreproc>{m['preproc']}</M_ImagePreproc>
      <SmoothTimes>{m['smooth']}</SmoothTimes>
      <SmoothTimes2>{m['smooth2']}</SmoothTimes2>
      <DarkThreshold>{th['dark']}</DarkThreshold>
      <BrightThreshold>{th['bright']}</BrightThreshold>
      <SobelDetectEnable>false</SobelDetectEnable>
      <SobelSmoothTimes>1</SobelSmoothTimes>
      <SobelSmoothTimes2>0</SobelSmoothTimes2>
      <SobelDarkThreshold>-20</SobelDarkThreshold>
      <SobelBrightThreshold>20</SobelBrightThreshold>
      <AlgorithmWay>8-Way-Star</AlgorithmWay>
      <AlgorithmCompare>{m['compare']}</AlgorithmCompare>
      <M_AlgorithmWayCompare>{m['awc']}</M_AlgorithmWayCompare>
      <Adjustment />
      <PitchTime>{m['pitch_time']}</PitchTime>
      <ChooseAmount>{m['choose']}</ChooseAmount>
      <EnableMultiscale>{m['multiscale']}</EnableMultiscale>
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


def make_recipe(chips, ioi, pitch, th, search=(1, 1), mode='div', opt=None):
    """th：單一 dict（全部晶片同門檻）或 list（每顆晶片一組，混切用）。mode：div / sub / divvote（見 METHODS）。"""
    ths = th if isinstance(th, list) else [th] * len(chips)
    rois = ''.join(detect_roi(r, pitch, t, search, mode, opt) for r, t in zip(chips, ths))
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
