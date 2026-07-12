#!/usr/bin/env python3
"""
doctor.py — CF-AOI 一鍵環境健康檢查（新人 Day 1 建立信心用）

在任何一台機器跑，自動偵測角色並檢查對應依賴：
  通用：Python / git repo 與護欄 / 磁碟空間
  IP  ：nvcc(CUDA) / cmake / OpenCV / nlohmann·fmt / build 產物
  Grab：pylon SDK / libibverbs / RDMA 裝置與 MTU
  Control：dotnet SDK / appsettings.json 格式 / RecipeDir 可寫

用法：python3 scripts/doctor.py        # 全綠 = 環境 OK，放心開工
輸出：PASS(綠)/FAIL(紅)/SKIP(灰，該機器用不到的項目)；FAIL 附修法提示。
"""
import json, os, re, shutil, subprocess, sys

G, R, Y, D, N = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"
if not sys.stdout.isatty():
    G = R = Y = D = N = ""
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
results = {"PASS": 0, "FAIL": 0, "SKIP": 0}

def report(status, name, detail="", hint=""):
    results[status] += 1
    mark = {"PASS": G + "✔ PASS", "FAIL": R + "✘ FAIL", "SKIP": D + "– SKIP"}[status]
    print(f"  {mark}{N}  {name}" + (f"  {D}{detail}{N}" if detail else ""))
    if status == "FAIL" and hint:
        print(f"         {Y}↳ 修法：{hint}{N}")

def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, ""
    except Exception as e:
        return 1, str(e)

def section(title):
    print(f"\n{title}")

# ── 通用 ─────────────────────────────────────────────────────────────────────
section("【通用】")
v = sys.version_info
report("PASS" if v >= (3, 8) else "FAIL", f"Python {v.major}.{v.minor}.{v.micro}",
       "驗證腳本需 ≥3.8", "安裝 python3.8+（實測環境 3.12）")

rc, out = run(["git", "-C", ROOT, "rev-parse", "--short", "HEAD"])
report("PASS" if rc == 0 else "FAIL", "git repo", f"HEAD={out}" if rc == 0 else "",
       "確認在 cf-aoi 目錄內、git 已安裝")
rc, out = run(["git", "-C", ROOT, "config", "pull.ff"])
report("PASS" if out == "only" else "FAIL", "git 護欄 pull.ff=only", out or "(未設)",
       "git config --global pull.ff only（docs/CLAUDE.md §10）")

st = shutil.disk_usage(ROOT)
free_gb = st.free / 1e9
report("PASS" if free_gb > 20 else "FAIL", f"磁碟剩餘 {free_gb:.0f} GB",
       "", "output/_diag 無自動清理（審計 P3-1），清出 >20GB")

# ── IP（CUDA 機器）──────────────────────────────────────────────────────────
section("【IP：CUDA 檢測節點】")
nvcc = shutil.which("nvcc") or (os.path.exists("/usr/local/cuda/bin/nvcc") and "/usr/local/cuda/bin/nvcc")
if nvcc:
    rc, out = run([nvcc, "--version"])
    m = re.search(r"release ([\d.]+)", out)
    report("PASS", f"CUDA nvcc {m.group(1) if m else '?'}",
           "" if shutil.which("nvcc") else "在 /usr/local/cuda（記得加 PATH）")
    rc, out = run(["cmake", "--version"])
    m = re.search(r"(\d+)\.(\d+)", out)
    ok = m and (int(m.group(1)), int(m.group(2))) >= (3, 24)
    report("PASS" if ok else "FAIL", f"cmake {m.group(0) if m else '缺'}", "需 ≥3.24",
           "sudo apt install cmake")
    rc, out = run(["pkg-config", "--modversion", "opencv4"])
    report("PASS" if rc == 0 else "FAIL", f"OpenCV {out if rc == 0 else '缺'}", "",
           "sudo apt install libopencv-dev")
    hdr_json = any(os.path.exists(p + "/nlohmann/json.hpp") for p in ["/usr/include", "/usr/local/include"])
    hdr_fmt = any(os.path.exists(p + "/fmt/format.h") for p in ["/usr/include", "/usr/local/include"])
    report("PASS" if hdr_json and hdr_fmt else "FAIL", "nlohmann-json + fmt 標頭", "",
           "sudo apt install nlohmann-json3-dev libfmt-dev")
    binp = os.path.join(ROOT, "ip", "build", "cfaoi_ip")
    report("PASS" if os.path.exists(binp) else "FAIL", "ip/build/cfaoi_ip 已編譯", "",
           "cd ip && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j（G1 有全文）")
else:
    report("SKIP", "無 nvcc → 本機非 IP 節點（Mac/純 Control 機屬正常）")

# ── Grab（截取中心）──────────────────────────────────────────────────────────
section("【Grab：取像節點】")
pylon = os.environ.get("PYLON_ROOT") or ("/opt/pylon" if os.path.isdir("/opt/pylon") else None)
ib = os.path.isdir("/sys/class/infiniband") and os.listdir("/sys/class/infiniband")
if pylon or ib:
    report("PASS" if pylon else "FAIL", f"pylon SDK {pylon or '缺'}", "",
           "Basler 官網裝 pylon 26.x 到 /opt/pylon 或設 PYLON_ROOT")
    if ib:
        report("PASS", f"RDMA 裝置 {','.join(ib)}")
        rc, out = run(["ibv_devinfo"])
        active = "PORT_ACTIVE" in out
        report("PASS" if active else "FAIL", "RDMA port ACTIVE", "",
               "查線/查 IP：G2 重開機還原清單（MTU 兩端要一致）")
        rc, out = run(["sh", "-c", "ip -o link show | grep -E 'mtu 9000'"])
        report("PASS" if rc == 0 else "FAIL", "存在 MTU 9000 介面（jumbo）",
               "", "sudo ip link set dev <網卡> mtu 9000（G2）")
    else:
        report("FAIL", "libibverbs / RDMA 裝置", "無 /sys/class/infiniband",
               "sudo apt install libibverbs-dev librdmacm-dev；確認 ConnectX 網卡")
else:
    report("SKIP", "無 pylon 也無 RDMA 裝置 → 本機非 Grab 節點")

# ── Control ──────────────────────────────────────────────────────────────────
section("【Control：操作機】")
rc, out = run(["dotnet", "--version"])
if rc == 0:
    major = int(out.split(".")[0]) if out.split(".")[0].isdigit() else 0
    report("PASS" if major >= 8 else "FAIL", f"dotnet SDK {out}", "專案 target net8.0",
           "裝 .NET 8+ SDK")
    app = os.path.join(ROOT, "control", "src", "appsettings.json")
    try:
        cfg = json.load(open(app, encoding="utf-8"))
        report("PASS", "appsettings.json 格式正確",
               f"ActiveIpNode={cfg.get('ActiveIpNode')}")
        rd = os.path.expanduser(cfg.get("Paths", {}).get("RecipeDir", "~/cf-aoi/recipes"))
        ok = os.path.isdir(rd) and os.access(rd, os.W_OK)
        report("PASS" if ok else "FAIL", f"RecipeDir 可寫 {rd}", "",
               f"mkdir -p {rd}（執行期配方目錄，非 repo recipes/）")
    except Exception as e:
        report("FAIL", "appsettings.json", str(e)[:60], "JSON 格式壞了——用 git diff 檢查最近改動")
else:
    report("SKIP", "無 dotnet → 本機不跑 Control")

# ── 總結 ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*56}")
tone = G if results['FAIL'] == 0 else R
print(f"{tone}體檢結果：PASS {results['PASS']} ／ FAIL {results['FAIL']} ／ SKIP {results['SKIP']}{N}")
if results["FAIL"] == 0:
    print(f"{G}✨ 全綠！環境健康，開工吧。下一步：G1 的 Hello World 沙盒任務。{N}")
else:
    print(f"{Y}照上面的 ↳ 修法逐項處理，修完重跑本腳本到全綠。{N}")
sys.exit(0 if results["FAIL"] == 0 else 1)
