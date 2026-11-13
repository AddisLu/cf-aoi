#!/usr/bin/env bash
# cf-aoi 共用資料夾（Samba）一鍵建置
#   分享名稱 : share      → Windows: \\<本機IP>\share
#   實體路徑 : /home/user/share
#   存取方式 : 免密碼(guest)可讀寫；另建 cfaoi 帳號當備援
#              （Windows 10/11 預設封鎖 guest，必要時用 cfaoi 帳號或開啟
#                AllowInsecureGuestAuth，見 tools/share/README.md）
# 用法: sudo bash tools/share/setup_smb_share.sh
set -euo pipefail

SHARE_DIR=/home/user/share
SHARE_NAME=share
OWNER=user
SMB_USER=cfaoi
CRED_FILE=/home/user/cfaoi-smb-account.txt

[[ $EUID -eq 0 ]] || { echo "請用 sudo 執行"; exit 1; }

echo "== 1/6 安裝 samba =="
if ! command -v smbd >/dev/null || ! command -v smbclient >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y samba smbclient
else
  echo "   已安裝，跳過"
fi

echo "== 2/6 建立資料夾 =="
mkdir -p "$SHARE_DIR"
chown "$OWNER:$OWNER" "$SHARE_DIR"
chmod 0775 "$SHARE_DIR"

echo "== 3/6 寫入 smb.conf =="
cp -n /etc/samba/smb.conf /etc/samba/smb.conf.orig 2>/dev/null || true
# 移除舊的 cf-aoi 區塊（可重複執行）
sed -i '/^# >>> cf-aoi share >>>/,/^# <<< cf-aoi share <<</d' /etc/samba/smb.conf
cat >> /etc/samba/smb.conf <<EOF
# >>> cf-aoi share >>>
[$SHARE_NAME]
   comment = cf-aoi shared folder
   path = $SHARE_DIR
   browseable = yes
   read only = no
   writable = yes
   guest ok = yes
   force user = $OWNER
   force group = $OWNER
   create mask = 0664
   force create mode = 0664
   directory mask = 0775
   force directory mode = 0775
   vfs objects = recycle
   recycle:repository = .recycle
   recycle:keeptree = yes
   recycle:versions = yes
# <<< cf-aoi share <<<
EOF

# [global] 必要項目
python3 - <<'PY'
import re
p='/etc/samba/smb.conf'
s=open(p).read()
s=re.sub(r'(?mi)^\s*hosts (allow|deny)\s*=.*\n','',s)  # 清掉舊版留下的，見下方註解
# 不要用 hosts allow/deny 做網段限制：曾經整個把 Windows 擋在門外，
# 而且拒絕訊息只在 log level 2 才看得到，排錯成本很高。
# 要限制範圍改用 interfaces + bind interfaces only（見 README）。
want={'map to guest':'Bad User',
      'server min protocol':'SMB2'}
for k,v in want.items():
    if re.search(r'(?mi)^\s*%s\s*='%re.escape(k), s):
        s=re.sub(r'(?mi)^\s*%s\s*=.*$'%re.escape(k), '   %s = %s'%(k,v), s, count=1)
    else:
        s=re.sub(r'(?mi)^\[global\]\s*$', '[global]\n   %s = %s'%(k,v), s, count=1)
open(p,'w').write(s)
PY

echo "== 4/6 建立備援帳號 $SMB_USER =="
id -u "$SMB_USER" >/dev/null 2>&1 || useradd -M -N -s /usr/sbin/nologin -G "$OWNER" "$SMB_USER"
if ! pdbedit -L 2>/dev/null | grep -q "^$SMB_USER:"; then
  # 不用 tr|head：pipefail 下 tr 吃 SIGPIPE 會讓 set -e 中止腳本
  # 避開現場容易打錯的字元：O/o/0、l/I/1、U/V 混淆，只用小寫+好認的數字
  PW=$(python3 -c "import secrets; a='abcdefghjkmnpqrstwxyz23456789'; print('cf-'+''.join(secrets.choice(a) for _ in range(8)))")
  printf '%s\n%s\n' "$PW" "$PW" | smbpasswd -s -a "$SMB_USER" >/dev/null
  smbpasswd -e "$SMB_USER" >/dev/null
  umask 077
  cat > "$CRED_FILE" <<EOF
cf-aoi Samba 備援帳號（Windows 若拒絕免密碼存取時使用）
  使用者: $SMB_USER
  密碼  : $PW
  路徑  : \\\\<本機IP>\\$SHARE_NAME
建立於: $(date '+%F %T')
EOF
  chown "$OWNER:$OWNER" "$CRED_FILE"; chmod 600 "$CRED_FILE"
  echo "   密碼已寫入 $CRED_FILE (chmod 600)"
  echo "   要改成自己好記的： sudo smbpasswd $SMB_USER"
else
  echo "   帳號已存在，保留原密碼"
fi

echo "== 5/6 啟動服務 =="
testparm -s >/dev/null
# samba 套件會順手啟用 AD DC 服務，這裡用不到，關掉免得開機報錯
systemctl disable --now samba-ad-dc >/dev/null 2>&1 || true
systemctl enable --now smbd nmbd
systemctl restart smbd nmbd

echo "== 6/6 防火牆 =="
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "^Status: active"; then
  ufw allow samba >/dev/null && echo "   ufw 已開放 samba"
else
  echo "   ufw 未啟用，無需調整"
fi

echo
echo "完成。Windows 檔案總管輸入："
ip -4 -o addr show scope global | awk -v s="$SHARE_NAME" '{split($4,a,"/"); printf "   \\\\%s\\%s\n", a[1], s}'
