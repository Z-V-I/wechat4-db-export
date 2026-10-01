#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxbak.py — 微信 4.x 本地聊天记录备份统一入口（Windows）

设计目标：零人工适配，换机器/换账号/换人有都能直接跑。
  * 数据目录：读微信官方配置 ini（权威） + 全盘符回退扫描，支持 --data-dir 手动指定
  * 消息分片：自动发现 message_*.db（不写死 0/1/2）
  * 多账号共存：密钥 / 解密产物按账号目录分开存放
  * 运行前自检：平台 / 依赖 / 权限 / 微信进程与版本 / 数据是否刚落盘
  * 密钥有效性自校验：page-1 HMAC，失效时给出分级诊断
  * 离线自检：`selftest` 用合成数据验证 解密 + WAL 重组 + 渲染，不需要微信

子命令：
  selftest                   离线自检（不碰微信，验证核心算法）
  doctor                     环境自检 + 诊断
  accounts                   列出所有账号
  extract                    从内存取密钥（自动发现目录）
  refresh                    解密数据库（含 -wal）
  list                       列出所有会话及条数
  export  --who A,B          导出指定会话
  all     --who A,B          一条龙：extract(按需) → refresh → export
"""
from __future__ import annotations

import os
import re
import sys
import json
import html
import struct
import sqlite3
import hashlib
import argparse
import subprocess
import shutil
import tempfile
from pathlib import Path
from datetime import datetime

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent

# 产物根目录：可用环境变量 WXBAK_HOME 重定位（默认放在 skill 内，保持自包含可搬运）
HOME_DIR = Path(os.environ.get("WXBAK_HOME") or HERE)
KEYS_DIR = HOME_DIR / "keys"
WORK_DIR = HOME_DIR / "work"

PAGE_SZ, SALT_SZ, IV_SZ, RESERVE_SZ = 4096, 16, 16, 80
SQLITE_HDR = b"SQLite format 3\x00"
ZERO = b"\x00" * RESERVE_SZ

# 经验证可用的微信版本前缀（仅用于 doctor 提示，不阻断运行）
TESTED_VERSIONS = ("4.1.11", "4.1.15")

# 微信安装位置候选（用于读版本号）
INSTALL_HINTS = (
    r"C:\Program Files\Tencent\Weixin",
    r"C:\Program Files (x86)\Tencent\Weixin",
    r"D:\Program Files\Tencent\Weixin",
    r"D:\Program Files (x86)\Tencent\Weixin",
)

try:
    from Crypto.Cipher import AES
except ImportError:                                   # pragma: no cover
    AES = None
try:
    import zstandard
except ImportError:                                   # pragma: no cover
    zstandard = None


# ============================================================ 基础工具

def out(msg=""):
    print(msg, flush=True)


def ds(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")


def ue(s):
    return html.unescape(s) if isinstance(s, str) else s


def is_windows():
    return sys.platform == "win32"


def is_admin():
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def require_windows():
    if not is_windows():
        raise SystemExit(
            f"✗ 当前平台 {sys.platform} 不受支持。\n"
            "  本工具依赖 Windows 进程内存布局（Linux/macOS 的微信结构不同）。\n"
            "  欢迎按 SKILL.md「移植到其他平台」一节贡献实现。"
        )


# ============================================================ 微信安装版本

def wechat_versions():
    """返回本机已安装的微信版本号列表（从安装目录的版本号子目录读出）。"""
    found = []
    for base in INSTALL_HINTS:
        p = Path(base)
        if not p.is_dir():
            continue
        try:
            entries = list(p.iterdir())
        except Exception:
            continue
        for e in entries:
            if e.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+(\.\d+)?", e.name):
                if e.name not in found:
                    found.append(e.name)
    return found


# ============================================================ 数据目录发现

def _ini_data_roots():
    """读微信官方配置 %APPDATA%\\Tencent\\xwechat\\config\\*.ini 里的数据根目录。"""
    roots = []
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return roots
    cfg = Path(appdata) / "Tencent" / "xwechat" / "config"
    if not cfg.is_dir():
        return roots
    for ini in sorted(cfg.glob("*.ini")):
        text = ""
        for enc in ("utf-8", "gbk"):
            try:
                text = ini.read_text(encoding=enc, errors="strict")
                break
            except Exception:
                continue
        for line in text.splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            # 形如 "MyDocument:D:\Documents" —— 冒号前是键名（不是盘符）
            if ":" in line and not re.match(r"^[A-Za-z]:[\\/]", line):
                line = line.split(":", 1)[1].strip().strip('"')
            if line and Path(line).is_dir():
                roots.append(Path(line))
    return roots


def find_data_roots(extra=None):
    """返回所有可能的 xwechat_files 根目录（去重，官方 ini 优先）。"""
    roots, seen = [], set()

    def add(p):
        try:
            p = Path(p)
        except Exception:
            return
        if not p.is_dir():
            return
        k = str(p).lower().rstrip("\\/")
        if k in seen:
            return
        seen.add(k)
        roots.append(p)

    if extra:
        e = Path(extra)
        # 允许直接给 xwechat_files / 账号目录 / db_storage
        add(e)
        add(e / "xwechat_files")
        if e.name == "db_storage":
            add(e.parent.parent)

    for r in _ini_data_roots():
        add(r / "xwechat_files")
        add(r)

    home = Path.home()
    for base in (home / "Documents", home):
        add(base / "xwechat_files")
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        add(Path(f"{drive}:/Documents/xwechat_files"))
        add(Path(f"{drive}:/xwechat_files"))
    return roots


def latest_db_mtime(db_storage):
    newest = 0.0
    msg_dir = db_storage / "message"
    if msg_dir.is_dir():
        for p in msg_dir.glob("*.db"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
    return newest


def find_accounts(data_dir=None):
    """扫描所有根目录，返回含 db_storage 的账号目录。"""
    roots = find_data_roots(data_dir)
    accounts, seen = [], set()
    for root in roots:
        if not root.is_dir():
            continue
        candidates = []
        if (root / "db_storage").is_dir():            # root 本身是账号目录
            candidates.append(root)
        else:
            try:
                candidates.extend(sorted(d for d in root.iterdir() if d.is_dir()))
            except Exception:
                continue
        for d in candidates:
            dbst = d / "db_storage"
            if not dbst.is_dir():
                continue
            key = str(d).lower()
            if key in seen:
                continue
            seen.add(key)
            name = d.name
            wxid = name.rsplit("_", 1)[0] if re.search(r"_[0-9a-f]{4,}$", name) else name
            accounts.append({
                "dir": d,
                "name": name,
                "wxid": wxid,
                "db_storage": dbst,
                "mtime": latest_db_mtime(dbst),
            })
    accounts.sort(key=lambda a: a["mtime"], reverse=True)
    return accounts


def pick_account(accounts, want=None):
    if not accounts:
        raise SystemExit(
            "✗ 未找到任何微信账号目录（<数据根>\\xwechat_files\\<账号>\\db_storage）。\n"
            "  · 确认本机装的是微信 4.x 并至少登录过一次\n"
            "  · 数据可能被设置到了其他盘：用 --data-dir 手动指定\n"
            "  · 用 accounts 子命令查看实际发现结果"
        )
    if want:
        w = want.lower()
        for a in accounts:
            if w in (a["name"].lower(), a["wxid"].lower(), str(a["dir"]).lower()):
                return a
        raise SystemExit("✗ 找不到账号 " + want + "，可选："
                         + ", ".join(f'{a["name"]}({a["wxid"]})' for a in accounts))
    return accounts[0]                                  # mtime 最新 = 当前活跃


def keys_file(acc):
    return KEYS_DIR / f'{acc["name"]}.json'


def msg_db_names(acc, out_root=None):
    """自动发现消息分片，不写死 0/1/2。"""
    base = (Path(out_root) / "message") if out_root else (acc["db_storage"] / "message")
    if not base.is_dir():
        return []
    names = [p.name for p in base.glob("message_*.db") if not p.name.endswith(("-wal", "-shm"))]
    if not names:
        names = [n for n in ("message_0.db", "message_1.db", "message_2.db") if (base / n).exists()]
    # message_0 < message_1 < ... < message_10
    def idx(n):
        m = re.search(r"_(\d+)\.db$", n)
        return int(m.group(1)) if m else 9999
    return sorted(names, key=idx)


# ============================================================ SQLCipher

def verify_key(enc_key, page1):
    """page-1 HMAC 校验，确认密钥对该库有效。"""
    import hmac as hmac_mod
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    hm = hmac_mod.new(mac_key, page1[16:PAGE_SZ - RESERVE_SZ + SALT_SZ], hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == page1[PAGE_SZ - 64:PAGE_SZ]


def dec_page(key, page, pgno):
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        plain = AES.new(key, AES.MODE_CBC, iv).decrypt(page[SALT_SZ:PAGE_SZ - RESERVE_SZ])
        return SQLITE_HDR + plain + ZERO
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(page[:PAGE_SZ - RESERVE_SZ])
    return plain + ZERO


def enc_page(key, plain_page, pgno, salt, mac_key):
    """dec_page 的逆运算（仅 selftest 用）。"""
    import hmac as hmac_mod
    from Crypto.Random import get_random_bytes
    iv = get_random_bytes(IV_SZ)
    if pgno == 1:
        body = AES.new(key, AES.MODE_CBC, iv).encrypt(plain_page[SALT_SZ:PAGE_SZ - RESERVE_SZ])
        sealed = salt + body + iv
        mac_input = sealed[16:PAGE_SZ - RESERVE_SZ + SALT_SZ]
    else:
        body = AES.new(key, AES.MODE_CBC, iv).encrypt(plain_page[:PAGE_SZ - RESERVE_SZ])
        sealed = body + iv
        mac_input = sealed[:PAGE_SZ - RESERVE_SZ]
    hm = hmac_mod.new(mac_key, mac_input, hashlib.sha512)
    hm.update(struct.pack("<I", pgno))
    return sealed + hm.digest()


def dec_db(key, src, dst):
    data = Path(src).read_bytes()
    if len(data) < PAGE_SZ:
        return 0
    n = (len(data) + PAGE_SZ - 1) // PAGE_SZ
    buf = bytearray()
    for i in range(n):
        pg = data[i * PAGE_SZ:(i + 1) * PAGE_SZ]
        if not pg:
            break
        if len(pg) < PAGE_SZ:
            pg += b"\x00" * (PAGE_SZ - len(pg))
        buf += dec_page(key, pg, i + 1)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(bytes(buf))
    return n


def dec_wal(key, src, dst):
    """WAL：32B 头 + 每帧(24B 帧头 + 4096B 页)；头/帧头明文，页走 SQLCipher。"""
    data = Path(src).read_bytes()
    if len(data) < 32:
        return 0
    hdr = data[:32]
    if struct.unpack(">I", hdr[:4])[0] not in (0x377F0682, 0x377F0683):
        return 0
    frame = 24 + PAGE_SZ
    n = (len(data) - 32) // frame
    buf = bytearray(hdr)
    for i in range(n):
        off = 32 + i * frame
        fh = data[off:off + 24]
        pg = data[off + 24:off + 24 + PAGE_SZ]
        if len(pg) < PAGE_SZ:
            break
        pgno = struct.unpack(">I", fh[0:4])[0]
        buf += fh + dec_page(key, pg, pgno)
    Path(dst).write_bytes(bytes(buf))
    return n


# ============================================================ 子命令：selftest

def cmd_selftest(args):
    """离线自检：不依赖微信，验证 页解密 / WAL 重组 / 渲染 三条核心链路。"""
    require_deps()
    out("=" * 56)
    out(f"wxbak 离线自检  v{__version__}")
    out("=" * 56)
    fails = []

    key = bytes(range(32))
    salt = bytes(range(16, 32))
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    frame = 24 + PAGE_SZ
    NPAGES = 6

    # 构造一组纯文本页（页 1 带 SQLite 头）。注意：真实 SQLCipher 库每页
    # 只使用前 4016 字节（尾 80 字节被 IV+HMAC 占用），所以这里只比对
    # 前 4016 字节，并验证尾部被清零。
    rnd = hashlib.sha512(b"wxbak-selftest").digest()
    pages = []
    for i in range(1, NPAGES + 1):
        body = bytes((rnd[(i * 7 + j) % 64] ^ (j * 31 + i)) & 0xFF for j in range(PAGE_SZ))
        pages.append(body)
    pages[0] = SQLITE_HDR + pages[0][SALT_SZ:]

    # ---- ① 页加解密往返（同时验证长度、头、预留区）
    sealed = b"".join(enc_page(key, pg, i + 1, salt, mac_key) for i, pg in enumerate(pages))
    if len(sealed) != NPAGES * PAGE_SZ:
        fails.append(f"封装后长度不符：{len(sealed)} != {NPAGES * PAGE_SZ}")
    elif sealed[:SALT_SZ] != salt:
        fails.append("封装后首页盐不匹配")
    elif any(sealed[i * PAGE_SZ:(i + 1) * PAGE_SZ] == pages[i] for i in range(NPAGES)):
        fails.append("密文与明文相同 —— 根本没有加密")
    else:
        out(f"[1] 页封装格式                 ✓ ({NPAGES} 页，长度/盐/密文检查通过)")

    with tempfile.TemporaryDirectory(prefix="wxbak_selftest_") as td:
        td = Path(td)
        enc = td / "enc.db"
        enc.write_bytes(sealed)
        dec = td / "dec.db"
        n_pages = dec_db(key, enc, dec)
        got = dec.read_bytes()
        if n_pages != NPAGES or len(got) != NPAGES * PAGE_SZ:
            fails.append(f"解密页数不符：{n_pages} / {len(got)}B")
        else:
            bad = None
            for i in range(NPAGES):
                g = got[i * PAGE_SZ:(i + 1) * PAGE_SZ]
                if g[:PAGE_SZ - RESERVE_SZ] != pages[i][:PAGE_SZ - RESERVE_SZ]:
                    bad = f"第 {i + 1} 页正文不一致"
                    break
                if g[PAGE_SZ - RESERVE_SZ:] != ZERO:
                    bad = f"第 {i + 1} 页预留区未清零"
                    break
            if not got.startswith(SQLITE_HDR):
                bad = "首页 SQLite 头重建失败"
            if bad:
                fails.append("解密往返：" + bad)
            else:
                out(f"[2] 页解密往返                 ✓ ({NPAGES} 页正文逐字节一致，预留区正确清零)")

        # ---- ② 密钥校验：正确密钥通过、错误密钥拒绝、篡改被发现
        if not verify_key(key, enc.read_bytes()[:PAGE_SZ]):
            fails.append("正确密钥未通过 HMAC 校验")
        elif verify_key(bytes(32), enc.read_bytes()[:PAGE_SZ]):
            fails.append("错误密钥竟然通过了 HMAC 校验")
        elif verify_key(key, enc.read_bytes()[:200] + b"\x00" + enc.read_bytes()[201:PAGE_SZ]):
            fails.append("篡改首页后 HMAC 仍然通过")
        else:
            out("[3] 密钥校验 / 篡改检测        ✓ (正例通过，反例全部拒绝)")

    # ---- ③ WAL 帧往返：帧头明文、页数据加密
    nframes = 9
    # WAL 头固定 32 字节：magic(4) + 版本(4) + 页大小(4) + 检查点序号(4)
    #                      + salt1(4) + salt2(4) + 校验和1(4) + 校验和2(4)
    whdr = struct.pack(">IIIIIIII", 0x377F0682, 3007000, PAGE_SZ, 1,
                       0x11223344, 0x55667788, 0x9ABCDEF0, 0x12345678)
    wframes = bytearray()
    for i in range(nframes):
        pgno = (i % NPAGES) + 1
        fh = struct.pack(">IIIIII", pgno, 0, 0xDEADBEEF, 0x01020304, i, 0)
        src = pages[pgno - 1]
        # 第 1 页的前 16 字节是 SQLite 文件头，不能被扰动（解密端会原样重建）
        pg = src[:SALT_SZ] + bytes((b ^ (i * 13 + 7)) & 0xFF for b in src[SALT_SZ:])
        wframes += fh + pg
    wal_plain = whdr + bytes(wframes)

    with tempfile.TemporaryDirectory(prefix="wxbak_selftest_") as td2:
        td2 = Path(td2)
        enc_wal = td2 / "x.db-wal"
        wbuf = bytearray(wal_plain[:32])
        for i in range(nframes):
            off = 32 + i * frame
            fh = wal_plain[off:off + 24]
            pg = wal_plain[off + 24:off + 24 + PAGE_SZ]
            pgno = struct.unpack(">I", fh[0:4])[0]
            wbuf += fh + enc_page(key, pg, pgno, salt, mac_key)
        enc_wal.write_bytes(bytes(wbuf))

        dec_wal_path = td2 / "y.db-wal"
        m = dec_wal(key, enc_wal, dec_wal_path)
        back = dec_wal_path.read_bytes()
        problems = []
        if m != nframes:
            problems.append(f"帧数 {m} != {nframes}")
        if back[:32] != whdr:
            problems.append("WAL 头不一致")
        if len(back) != len(wal_plain):
            problems.append(f"长度 {len(back)} != {len(wal_plain)}")
        else:
            for i in range(nframes):
                off = 32 + i * frame
                if back[off:off + 24] != wal_plain[off:off + 24]:
                    problems.append(f"第 {i + 1} 帧帧头被改动")
                    break
                if back[off + 24:off + 24 + PAGE_SZ - RESERVE_SZ] != \
                        wal_plain[off + 24:off + 24 + PAGE_SZ - RESERVE_SZ]:
                    problems.append(f"第 {i + 1} 帧页正文不一致")
                    break
        if problems:
            fails.extend("WAL：" + p for p in problems)
        else:
            out(f"[4] WAL 帧解密往返             ✓ ({nframes} 帧；WAL 头/帧头原样、页正文一致)")

        # 非 WAL 文件必须被安全忽略
        bad_wal = td2 / "bad.db-wal"
        bad_wal.write_bytes(b"\x00" * 64)
        if dec_wal(key, bad_wal, td2 / "out.db-wal") != 0:
            fails.append("非 WAL 文件未被正确忽略")
        else:
            out("[5] 非 WAL 文件安全忽略        ✓")

    # ---- ④ 渲染逻辑（纯函数，覆盖几个真实踩过的坑）
    emo = {"cap": {}, "pk": {}, "md2pk": {}}
    cases = [
        (1, " 你好&#x20;世界 ", " 你好 世界 "),
        (3, "<msgsource/>", "[图片]"),
        (47, '<msg><emoji md5="0be51b683f05252685a24c4a644bf1d7"/></msg>', "[表情#0be51b68]"),
        (10000,
         '<sysmsg type="revokemsg"><revokemsg><content>"X" 撤回了一条消息</content>'
         '<revoketime>0</revoketime></revokemsg></sysmsg>',
         '"X" 撤回了一条消息'),
        (244813135921,
         '<msg><appmsg><title>我在回复</title><refermsg><fromusr>wxid_a</fromusr>'
         '<displayname>昵称</displayname><content>被引用的原话</content></refermsg>'
         '</appmsg></msg>',
         "我在回复  〔回复 昵称：被引用的原话〕"),
        # 嵌套且被转义的引用内容（必须 unescape→剥标签→循环）
        (244813135921,
         '<msg><appmsg><title>看这个</title><refermsg><fromusr>wxid_a</fromusr>'
         '<displayname>N</displayname><content>&lt;msg&gt;&lt;appmsg&gt;&lt;title&gt;内层标题'
         '&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>',
         "看这个  〔回复 N：内层标题〕"),
        (8589934592049, '<msg><appmsg><title><![CDATA[微信转账]]></title>'
                        '<des><![CDATA[请收款]]></des></appmsg></msg>',
         "微信转账  请收款"),
    ]
    bad = 0
    for lt, raw_s, want_s in cases:
        g = render(lt, raw_s, emo)
        if g != want_s:
            bad += 1
            fails.append(f"render(local_type={lt}) 期望 {want_s!r}，实得 {g!r}")
    if not bad:
        out(f"[6] 消息渲染                   ✓ ({len(cases)} 条用例全过)")

    # ---- ⑤ 若本机已有解密产物，顺带做一次真实数据冒烟
    real = []
    if WORK_DIR.is_dir():
        for acc_dir in WORK_DIR.iterdir():
            md = acc_dir / "message"
            if md.is_dir():
                real.extend(sorted(md.glob("message_*.db")))
    if real:
        tried = 0
        for p in real[:2]:
            c = sqlite3.connect(str(p))
            try:
                n = c.execute("SELECT COUNT(*) FROM sqlite_master "
                              "WHERE type='table' AND name LIKE 'Msg_%'").fetchone()[0]
                tried += 1
                out(f"[7] 真实库冒烟                 ✓ {p.parent.parent.name}/{p.name} 有 {n} 张消息表")
            except Exception as e:
                fails.append(f"真实库无法读取 {p.name}: {e}")
            finally:
                c.close()
        if not tried:
            out("[7] 真实库冒烟                 — (无已解密数据，跳过)")
    else:
        out("[7] 真实库冒烟                 — (无已解密数据，先运行 refresh 再自检)")

    out("-" * 56)
    if fails:
        out("✗ 自检未通过：")
        for f in fails:
            out(f"    - {f}")
        return 1
    out("✓ 全部通过 —— 核心算法在本机可正常工作，可以继续用 doctor / all")
    return 0


def require_deps():
    missing = []
    if not AES:
        missing.append("pycryptodome")
    if not zstandard:
        missing.append("zstandard")
    if missing:
        raise SystemExit(
            "✗ 缺少依赖：" + ", ".join(missing) + "\n"
            "  安装：python -m pip install -r requirements.txt\n"
            "  （或 pip install " + " ".join(missing) + "）"
        )


# ============================================================ 子命令：doctor

def cmd_doctor(args):
    out("=" * 56)
    out(f"wxbak 环境自检  v{__version__}")
    out("=" * 56)
    problems, warnings = [], []

    out("\n[1] 运行环境")
    out(f"    平台         : {sys.platform}" + ("" if is_windows() else "  ✗ 仅支持 Windows"))
    out(f"    Python       : {sys.version.split()[0]}")
    out(f"    pycryptodome : {'✓' if AES else '✗ 缺失 → pip install pycryptodome'}")
    out(f"    zstandard    : {'✓' if zstandard else '✗ 缺失 → pip install zstandard'}")
    if not is_windows():
        problems.append("当前平台不是 Windows，本工具无法运行")
    if not AES:
        problems.append("缺少 pycryptodome")
    if not zstandard:
        problems.append("缺少 zstandard")

    out("\n[2] 权限")
    adm = is_admin()
    out(f"    管理员: {'✓ 是' if adm else '✗ 否'}")
    if is_windows() and not adm:
        warnings.append("非管理员：读取微信进程内存会失败，需以管理员身份运行")

    out("\n[3] 微信进程")
    pids = _list_wechat_pids()
    if pids:
        out(f"    ✓ 正在运行，{len(pids)} 个进程（主进程 PID={pids[0][0]}, {pids[0][1] // 1024}MB）")
    else:
        out("    ✗ 未运行 —— 取密钥前必须先启动并登录微信")
        problems.append("微信未运行")

    out("\n[4] 微信版本")
    vers = wechat_versions()
    if vers:
        for v in vers:
            hit = any(v.startswith(t) for t in TESTED_VERSIONS)
            out(f"    {'✓' if hit else '⚠'} {v}" + ("" if hit else "  ← 未经测试，可能需要重新逆向内存结构"))
        if not any(v.startswith(t) for t in TESTED_VERSIONS):
            warnings.append("微信版本不在已验证列表（" + ", ".join(TESTED_VERSIONS)
                            + "）内；若取密钥失败，见 SKILL.md「版本漂移修复」")
    else:
        out("    - 未在常见安装目录找到版本号（不影响使用）")

    out("\n[5] 账号目录")
    accounts = find_accounts(args.data_dir)
    if not accounts:
        out("    ✗ 未发现 xwechat_files/*/db_storage")
        out("      可用 --data-dir 指定数据根目录")
        return _finish(problems, warnings)
    for a in accounts:
        ts = datetime.fromtimestamp(a["mtime"]).strftime("%Y-%m-%d %H:%M") if a["mtime"] else "无数据"
        has_key = "已有密钥" if keys_file(a).exists() else "无密钥"
        out(f"    {'★' if a is accounts[0] else ' '} {a['name']:<30} 活跃 {ts}  [{has_key}]")
    out("    ★ = 当前活跃账号（未指定 --account 时自动选中）")

    acc = accounts[0]
    out(f"\n[6] 数据新鲜度（{acc['name']}）")
    now = datetime.now().timestamp()
    for name in msg_db_names(acc):
        p = acc["db_storage"] / "message" / name
        if not p.exists():
            continue
        st = p.stat()
        age = (now - st.st_mtime) / 60
        wal = p.with_name(p.name + "-wal")
        wal_sz = wal.stat().st_size if wal.exists() else 0
        fresh = "⚠ 刚刚写入，可能未落盘" if age < 2 else ""
        out(f"    {name:<16} {st.st_size / 1024:>8.0f}KB  {age:>6.1f}分钟前  WAL={wal_sz / 1024:>7.0f}KB {fresh}")
        if wal_sz > 1024 * 1024:
            warnings.append(f"{name} 的 WAL 有 {wal_sz / 1024 / 1024:.1f}MB —— "
                            "新消息大多还在 WAL 里，必须一并解密")
        if age < 2:
            warnings.append(f"{name} 刚被写入，此刻快照可能不含最新消息，建议稍等重跑")

    kf = keys_file(acc)
    if kf.exists():
        out(f"\n[7] 密钥有效性（{acc['name']}）")
        try:
            keys = json.loads(kf.read_text(encoding="utf-8"))
        except Exception as e:
            out(f"    ✗ 密钥文件损坏：{e}")
            problems.append("密钥文件损坏 → 重新 extract")
            return _finish(problems, warnings)
        okn = badn = 0
        for rel, v in keys.items():
            src = acc["db_storage"] / rel
            if not src.exists():
                continue
            if verify_key(bytes.fromhex(v["enc_key"]), src.read_bytes()[:PAGE_SZ]):
                okn += 1
            else:
                badn += 1
                out(f"    ✗ {rel}")
        out(f"    ✓ {okn} 个库密钥有效，{badn} 个失效")
        if badn and okn == 0:
            problems.append("全部密钥失效 → 运行 extract --force 重新取")
        elif badn:
            warnings.append(f"{badn} 个库密钥失效（多为无关库），如涉及 message_*.db 请 extract --force")
        elif okn:
            out("    → 密钥可用，无需重新扫内存，直接 refresh 即可")
    else:
        out(f"\n[7] 密钥\n    - 尚无密钥，首次运行 all / extract 时自动获取")

    return _finish(problems, warnings)


def _finish(problems, warnings):
    out("\n" + "=" * 56)
    if not problems:
        out("✓ 自检通过，可以执行 refresh / all")
    else:
        out("✗ 存在问题：")
        for p in problems:
            out(f"    - {p}")
    if warnings:
        out("\n提示：")
        for w in warnings:
            out(f"    - {w}")
    out("=" * 56)
    return 1 if problems else 0


def _list_wechat_pids():
    pids = []
    if not is_windows():
        return pids
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in r.stdout.strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 5 and parts[1].isdigit():
                pids.append((int(parts[1]),
                             int(parts[4].replace(",", "").replace(" K", "").strip() or 0)))
    except Exception:
        pass
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


# ============================================================ 子命令：accounts

def cmd_accounts(args):
    accounts = find_accounts(args.data_dir)
    if not accounts:
        out("未发现任何账号。可用 --data-dir <数据根目录> 指定。")
        return
    for a in accounts:
        ts = datetime.fromtimestamp(a["mtime"]).strftime("%Y-%m-%d %H:%M") if a["mtime"] else "-"
        out(f'{a["name"]:<32} wxid={a["wxid"]:<26} 活跃={ts}')
        out(f'    {a["db_storage"]}')


# ============================================================ 子命令：extract

def cmd_extract(args):
    require_windows()
    acc = pick_account(find_accounts(args.data_dir), args.account)
    kf = keys_file(acc)
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    out(f"[*] 账号    : {acc['name']}")
    out(f"[*] 数据目录: {acc['db_storage']}")

    if args.keyfile:
        shutil.copy(args.keyfile, kf)
        out(f"[✓] 已导入密钥 {args.keyfile} -> {kf}")
        return

    if kf.exists() and not args.force:
        keys = json.loads(kf.read_text(encoding="utf-8"))
        okn = 0
        for rel, v in keys.items():
            src = acc["db_storage"] / rel
            if src.exists() and verify_key(bytes.fromhex(v["enc_key"]), src.read_bytes()[:PAGE_SZ]):
                okn += 1
        if okn:
            out(f"[✓] 已有密钥仍然有效（{okn} 个库），跳过内存扫描。强制重取加 --force")
            return

    if not is_admin():
        out("[!] 警告：当前不是管理员，读取进程内存大概率失败。建议提权后重试。")

    wcdb = HERE / "wcdb_key.py"
    if not wcdb.exists():
        raise SystemExit(f"✗ 缺少 {wcdb}")
    cmd = [sys.executable, str(wcdb), "extract",
           "--db-dir", str(acc["db_storage"]), "--output", str(kf)]
    out(f"[*] 运行内存扫描: {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0 or not kf.exists():
        _diagnose_extract_failure(acc)
        raise SystemExit("✗ 取密钥失败")
    keys = json.loads(kf.read_text(encoding="utf-8"))
    out(f"[✓] 取得 {len(keys)} 个库的密钥 -> {kf}")


def _diagnose_extract_failure(acc):
    out("\n" + "!" * 56)
    out("取密钥失败 · 分级诊断（按顺序排查）")
    out("!" * 56)
    out("  ① 微信没运行 / 没登录")
    out("     → 启动微信并完成登录，再重试（密钥只在登录后加载进内存）")
    if not is_admin():
        out("  ② 不是管理员   ← 当前就是这个状态")
        out("     → 以管理员身份重新运行")
    else:
        out("  ② 权限：已是管理员，此项排除")
    vers = wechat_versions()
    out(f"  ③ 微信版本变了（最可能）  本机版本：{', '.join(vers) if vers else '未知'}")
    out(f"     已验证版本：{', '.join(TESTED_VERSIONS)}")
    out("     → 若版本号变大（如 4.2.x），wcdb_key.py 中这几个常量需重新逆向：")
    out("        · WINDOWS_CONFIG_XOR_MASK（XOR 掩码）")
    out("        · 命中处取 config_ptr 的偏移 node+0x28")
    out("        · 读对象偏移 config_ptr+0x88 与对象长度 0x28")
    out("     → 参考 SKILL.md「版本漂移修复」")
    out("  ④ 内存里 Cipher 对象尚未构造（刚启动/刚更新完）")
    out("     → 在微信里点开任意一个聊天窗口，再重试")
    out("  ⑤ 只支持微信 4.1+；4.0 的内存结构与 4.1 不同")
    out("!" * 56)


# ============================================================ 子命令：refresh

def cmd_refresh(args):
    require_deps()
    acc = pick_account(find_accounts(args.data_dir), args.account)
    kf = keys_file(acc)
    if not kf.exists():
        raise SystemExit(f"✗ 没有密钥文件 {kf}，先运行 extract")
    keys = json.loads(kf.read_text(encoding="utf-8"))
    dst_root = Path(args.out) if args.out else WORK_DIR / acc["name"]
    if dst_root.exists():
        shutil.rmtree(dst_root)

    ok, miss, stale = [], [], []
    for root, _, files in os.walk(acc["db_storage"]):
        for f in files:
            if not f.endswith(".db") or f.endswith("-shm"):
                continue
            src = Path(root) / f
            rel = str(src.relative_to(acc["db_storage"])).replace("/", "\\")
            v = keys.get(rel) or keys.get(rel.replace("\\", "/"))
            if not v:
                miss.append(rel)
                continue
            enc_key = bytes.fromhex(v["enc_key"])
            if not verify_key(enc_key, src.read_bytes()[:PAGE_SZ]):
                stale.append(rel)
                continue
            dst = dst_root / rel.replace("\\", "/")
            np = dec_db(enc_key, src, dst)
            wal_n = 0
            wsrc = src.with_name(src.name + "-wal")
            if wsrc.exists() and wsrc.stat().st_size > 32:
                wal_n = dec_wal(enc_key, wsrc, dst.with_name(dst.name + "-wal"))
            ok.append((rel, np, wal_n))
            out(f"  ✅ {rel:<40} {np:>6}页  WAL {wal_n:>5}帧")

    out()
    if stale:
        out(f"⚠️ {len(stale)} 个库密钥失效（微信可能重装/换账号）：{stale}")
        out("   → 运行 extract --force 重新取密钥")
    if miss:
        out(f"ℹ️ {len(miss)} 个库无密钥，已跳过（多为无关库）: {miss[:5]}")
    if not ok:
        raise SystemExit("✗ 没有任何库解密成功，运行 doctor 排查")
    out(f"[✓] 解密 {len(ok)} 个库 -> {dst_root}")

    # 落盘校验：主库快照 size 应与源库一致（否则可能读到半写状态）
    for rel, _, _ in ok:
        if "message" not in rel:
            continue
        src = acc["db_storage"] / rel
        snap = dst_root / rel.replace("\\", "/")
        if src.stat().st_size != snap.stat().st_size:
            out(f"⚠️ {rel} 快照与源库大小不一致，微信可能正在写入，建议稍后重跑")
            break


# ============================================================ 读取解密库

def _msg_conn(out_root, dbf):
    p = Path(out_root) / "message" / dbf
    if not p.exists():
        return None
    c = sqlite3.connect(str(p))
    c.text_factory = bytes
    return c


def _all_msg_tables(acc, out_root):
    """返回 {md5: 总行数}，跨所有消息分片。"""
    counts = {}
    for dbf in msg_db_names(acc, out_root):
        c = _msg_conn(out_root, dbf)
        if not c:
            continue
        try:
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
                t = ds(t)
                counts[t[4:]] = counts.get(t[4:], 0) + c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.DatabaseError:
            pass
        c.close()
    return counts


def _table_cols(c, table):
    try:
        return [ds(r[1]) for r in c.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.DatabaseError:
        return []


def _load_contact(acc, out_root):
    """返回 (by_wxid, by_md5)。列名做存在性判断，兼容不同微信版本的缺列。"""
    p = Path(out_root) / "contact" / "contact.db"
    if not p.exists():
        return {}, {}
    c = sqlite3.connect(str(p))
    c.text_factory = bytes
    cols = _table_cols(c, "contact")
    if "username" not in cols:
        c.close()
        return {}, {}
    iu = cols.index("username")
    ir = cols.index("remark") if "remark" in cols else -1
    inn = cols.index("nick_name") if "nick_name" in cols else -1
    ia = cols.index("alias") if "alias" in cols else -1
    by_wxid, by_md5 = {}, {}
    for r in c.execute("SELECT * FROM contact"):
        if iu >= len(r):
            continue
        u = ds(r[iu])
        if not u:
            continue
        rec = {
            "remark": ds(r[ir]) if 0 <= ir < len(r) else "",
            "nick": ds(r[inn]) if 0 <= inn < len(r) else "",
            "alias": ds(r[ia]) if 0 <= ia < len(r) else "",
        }
        by_wxid[u] = rec
        by_md5[hashlib.md5(u.encode()).hexdigest()] = (u, rec)
    c.close()
    return by_wxid, by_md5


def _load_rooms(acc, out_root):
    """群聊：room_id -> 成员 wxid 集合（用于区分群聊、解析成员名）。"""
    p = Path(out_root) / "contact" / "contact.db"
    rooms = {}
    if not p.exists():
        return rooms
    c = sqlite3.connect(str(p))
    c.text_factory = bytes
    cols = _table_cols(c, "chat_room")
    if "username" in cols:
        try:
            for r in c.execute("SELECT * FROM chat_room"):
                rooms[ds(r[cols.index("username")])] = {"members": set()}
        except sqlite3.DatabaseError:
            pass
    cols = _table_cols(c, "chatroom_member")
    if "room_id" in cols and "member_id" in cols:
        try:
            for r in c.execute("SELECT * FROM chatroom_member"):
                rid, mid = ds(r[cols.index("room_id")]), ds(r[cols.index("member_id")])
                rooms.setdefault(rid, {"members": set()})["members"].add(mid)
        except sqlite3.DatabaseError:
            pass
    c.close()
    return rooms


def _load_emoji(acc, out_root):
    """表情说明：按列名取，避免不同版本列序不同导致取错。"""
    p = Path(out_root) / "emoticon" / "emoticon.db"
    if not p.exists():
        return {"cap": {}, "pk": {}, "md2pk": {}}
    c = sqlite3.connect(str(p))
    c.text_factory = bytes
    cap, pkg, md2pk = {}, {}, {}

    def pick(rows, table, md_cands, cap_cands):
        cols = _table_cols(c, table)
        if not cols:
            return
        mi = next((cols.index(x) for x in md_cands if x in cols), None)
        ci = next((cols.index(x) for x in cap_cands if x in cols), None)
        if mi is None or ci is None:
            return
        for r in rows:
            if max(mi, ci) >= len(r):
                continue
            md, cv = ds(r[mi]).lower(), ds(r[ci])
            if md and cv and md not in cap:
                cap[md] = cv

    try:
        cols = _table_cols(c, "kStoreEmoticonPackageTable")
        if cols:
            i0 = next((cols.index(x) for x in ("package_id_", "package_id") if x in cols), None)
            i1 = next((cols.index(x) for x in ("package_name_", "package_name", "name") if x in cols), None)
            if i0 is not None and i1 is not None:
                pkg = {ds(r[i0]): ds(r[i1]) for r in c.execute("SELECT * FROM kStoreEmoticonPackageTable")}
    except sqlite3.DatabaseError:
        pass
    try:
        cols = _table_cols(c, "kStoreEmoticonFilesTable")
        if cols:
            i0 = next((cols.index(x) for x in ("package_id_", "package_id") if x in cols), None)
            i1 = next((cols.index(x) for x in ("md5_", "md5") if x in cols), None)
            if i0 is not None and i1 is not None:
                md2pk = {ds(r[i1]): ds(r[i0]) for r in c.execute("SELECT * FROM kStoreEmoticonFilesTable")}
    except sqlite3.DatabaseError:
        pass

    for tbl in ("kStoreEmoticonCaptionsTable", "kNonStoreEmoticonTable"):
        try:
            pick(c.execute(f'SELECT * FROM "{tbl}"'), tbl, ("md5_", "md5"), ("caption_", "caption"))
        except sqlite3.DatabaseError:
            pass
    c.close()
    return {"cap": cap, "pk": pkg, "md2pk": md2pk}


TYPE_NAME = {3: "图片", 34: "语音", 42: "名片", 43: "视频", 48: "位置",
             50: "通话", 64: "语音通话", 66: "视频通话"}
LINK_TYPES = (21474836529, 34359738417, 81604378673, 8589934592049, 8594229559345)


def unz(c, flag):
    if not isinstance(c, (bytes, bytearray)):
        return c
    if flag == 4:
        if not zstandard:
            return None
        try:
            return zstandard.ZstdDecompressor().decompress(bytes(c)).decode("utf-8", "replace")
        except Exception:
            return None
    try:
        return bytes(c).decode("utf-8", "replace")
    except Exception:
        return None


_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip(x):
    """反复「还原实体 + 剥标签」直到稳定。

    必须循环：被引用的内容可能是一段**被转义的** XML（`&lt;msg&gt;...`），
    先剥标签是剥不掉的（那时还是 `&lt;`），必须先 unescape 再剥。
    """
    if not x:
        return ""
    x = _CDATA_RE.sub(r"\1", x)
    for _ in range(4):
        y = ue(_TAG_RE.sub("", x))
        if y == x:
            break
        x = y
    return x.strip()


def _field(x, tag):
    """取 <tag>...</tag> 的原始内层（不剥标签，供嵌套场景继续解析）。"""
    m = re.search(r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (tag, tag), x, re.S)
    return m.group(1) if m else ""


def _quote(raw):
    """被引用的内容可能本身是富媒体 XML，尽量取到可读文本。"""
    if not raw:
        return ""
    x = ue(_CDATA_RE.sub(r"\1", raw))
    if "<" in x:
        t = _strip(_field(x, "title"))
        if t:
            return t
        r = _field(x, "refermsg")
        if r:
            return _strip(_field(r, "content"))
    return _strip(x)


def render(lt, content, emo):
    if content is None:
        return None
    if lt == 1:
        return ue(content)
    if lt == 47:
        m = re.search(r'md5="([0-9a-fA-F]{32})"', content)
        if not m:
            return "[表情]"
        md = m.group(1).lower()
        short = md[:8]
        cv = emo["cap"].get(md)
        pk = emo["pk"].get(emo["md2pk"].get(md, ""))
        if cv:
            return f"[表情#{short} 说明：{cv}]"
        if pk:
            return f"[表情#{short} 包：{pk}]"
        return f"[表情#{short}]"
    if lt == 244813135921:
        # 引用回复：<title>=自己回复的话；<refermsg> 里是被引用的原文
        reply = _strip(_field(content, "title"))
        ref = _field(content, "refermsg")
        if ref:
            fru = _strip(_field(ref, "fromusr"))
            resolve = emo.get("resolve")
            dn = (resolve(fru) if (resolve and fru) else "") or _strip(_field(ref, "displayname"))
            quoted = _quote(_field(ref, "content"))
            if quoted:
                q = f"〔回复 {dn}：{quoted}〕" if dn else f"〔回复：{quoted}〕"
                return f"{reply}  {q}" if reply else q
        return reply or "[引用消息]"
    if lt == 10000:
        for pat in (r"<replacemsg><!\[CDATA\[(.*?)\]\]></replacemsg>",
                    r"<replacemsg>(.*?)</replacemsg>", r"<content>(.*?)</content>"):
            m = re.search(pat, content, re.S)
            if m:
                return ue(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        return ue(re.sub(r"<[^>]+>", "", content)).strip()
    if lt in TYPE_NAME:
        return f"[{TYPE_NAME[lt]}]"
    if lt == 34359738417:
        return "[文件]"
    # 其余富媒体（链接/转账/红包/合并转发/小程序等）：都靠 title + des
    ti = _strip(_field(content, "title"))
    de = _strip(_field(content, "des"))
    if ti and de:
        return f"{ti}  {de}"
    if ti:
        return ti
    if de:
        return de
    if lt in LINK_TYPES:
        return "[链接]"
    return f"[类型{lt}]"


def _read_conversation(acc, out_root, wxid):
    tname = "Msg_" + hashlib.md5(wxid.encode()).hexdigest()
    rows = []
    for dbf in msg_db_names(acc, out_root):
        c = _msg_conn(out_root, dbf)
        if not c:
            continue
        q = (f'SELECT m.sort_seq,n.user_name,m.create_time,m.local_type,'
             f'm.message_content,m.WCDB_CT_message_content '
             f'FROM "{tname}" m LEFT JOIN Name2Id n ON m.real_sender_id=n.rowid')
        try:
            for seq, who, tt, lt, content, flag in c.execute(q):
                rows.append((tt or 0, seq or 0, ds(who), lt, unz(content, flag)))
        except sqlite3.DatabaseError:
            pass
        c.close()
    rows.sort(key=lambda x: (x[0], x[1]))
    return rows


# ============================================================ 子命令：list / export

def _conversations(acc, out_root):
    """返回 [(条数, 显示名, wxid, 是否群聊)]，按条数降序。"""
    counts = _all_msg_tables(acc, out_root)
    by_wxid, by_md5 = _load_contact(acc, out_root)
    rooms = _load_rooms(acc, out_root)
    rows = []
    for md5h, n in counts.items():
        hit = by_md5.get(md5h)
        if hit:
            wxid, rec = hit
            disp = rec["remark"] or rec["nick"] or wxid
        else:
            wxid, rec, disp = f"(未知 md5:{md5h[:8]})", {}, f"(未知 md5:{md5h[:8]})"
        rows.append((n, disp, wxid, wxid in rooms or wxid.endswith("@chatroom")))
    rows.sort(reverse=True)
    return rows


def cmd_list(args):
    acc = pick_account(find_accounts(args.data_dir), args.account)
    out_root = Path(args.src) if args.src else WORK_DIR / acc["name"]
    if not out_root.exists():
        raise SystemExit(f"✗ 解密目录不存在 {out_root}，先运行 refresh")
    rows = _conversations(acc, out_root)
    out(f"{'会话':<24}{'条数':>8}  类型   wxid")
    out("-" * 78)
    for n, disp, wxid, is_room in rows:
        kind = "群聊" if is_room else ("我" if wxid == acc["wxid"] else "单聊")
        out(f"{disp:<24}{n:>8}  {kind}   {wxid}")
    out("-" * 78)
    out(f"共 {len(rows)} 个会话，{sum(n for n, _, _, _ in rows)} 条消息")


def cmd_export(args):
    require_deps()
    acc = pick_account(find_accounts(args.data_dir), args.account)
    out_root = Path(args.src) if args.src else WORK_DIR / acc["name"]
    if not out_root.exists():
        raise SystemExit(f"✗ 解密目录不存在 {out_root}，先运行 refresh")
    by_wxid, by_md5 = _load_contact(acc, out_root)
    rooms = _load_rooms(acc, out_root)
    emo = _load_emoji(acc, out_root)

    def _resolve(wxid):
        """把 wxid 翻成显示名，保证引用块里的称呼与正文一致。"""
        if wxid == acc["wxid"]:
            return args.self_name
        ci = by_wxid.get(wxid, {})
        return ci.get("remark") or ci.get("nick") or wxid

    emo["resolve"] = _resolve

    # 解析 --who：备注名 / 昵称 / 微信号 / wxid 都认（大小写不敏感）
    targets = []
    if args.who:
        wanted = [w.strip() for w in args.who.split(",") if w.strip()]
        for w in wanted:
            lw = w.lower()
            found = None
            for wxid, rec in by_wxid.items():
                vals = [wxid, rec.get("remark", ""), rec.get("nick", ""), rec.get("alias", "")]
                if any(lw == str(v).lower() for v in vals if v):
                    found = (rec.get("remark") or rec.get("nick") or wxid, wxid)
                    break
            if not found:
                # 允许直接给 wxid（联系人表里没有也能导）
                if w.endswith("@chatroom") or w.startswith("wxid_"):
                    found = (w, w)
                else:
                    out(f"[!] 认不出会话「{w}」，跳过（用 list 查看全部可选值）")
                    continue
            targets.append(found)
    else:
        targets = [(disp, wxid) for _, disp, wxid, _ in _conversations(acc, out_root)]

    outdir = Path(args.out) if args.out else (HOME_DIR / "导出")
    outdir.mkdir(parents=True, exist_ok=True)

    summary = []
    for disp, wxid in targets:
        if wxid == acc["wxid"]:
            continue
        rows = _read_conversation(acc, out_root, wxid)
        if not rows:
            continue
        rec = by_wxid.get(wxid, {})
        is_room = wxid in rooms or wxid.endswith("@chatroom")
        body, nemoji, nhit, json_rows = [], 0, 0, []
        for tt, seq, who, lt, txt in rows:
            if who == acc["wxid"]:
                w = args.self_name
            elif who in (None, "", "0"):
                w = "系统"
            else:
                ci = by_wxid.get(who, {})
                w = ci.get("remark") or ci.get("nick") or who
            if lt == 10000 or (txt and "我通过了你的朋友验证请求" in txt):
                w = "系统"
            if lt == 47:
                nemoji += 1
            txt = render(lt, txt, emo) if txt is not None else render(lt, "", emo)
            if "说明：" in (txt or ""):
                nhit += 1
            ts = datetime.fromtimestamp(tt).strftime("%Y-%m-%d %H:%M") if tt else "----"
            body.append(f"[{ts}] {w}：{txt or ''}")
            json_rows.append({"time": ts, "ts": tt, "speaker": w, "type": lt,
                              "text": txt or "", "is_self": who == acc["wxid"]})

        safe = re.sub(r'[\\/:*?"<>|]', "_", disp) or "会话"
        if args.format == "json":
            fp = outdir / f"{safe}.json"
            fp.write_text(json.dumps({
                "conversation": disp, "kind": "群聊" if is_room else "单聊",
                "remark": rec.get("remark", ""), "nick": rec.get("nick", ""),
                "alias": rec.get("alias", ""), "wxid": wxid,
                "count": len(rows), "messages": json_rows,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            head = [
                f"# 与 {disp} 的聊天记录",
                f"# 备注：{rec.get('remark','')}    昵称：{rec.get('nick','')}    微信号：{rec.get('alias','')}",
                f"# wxid：{wxid}    类型：{'群聊' if is_room else '单聊'}    消息数：{len(rows)}    "
                f"时间：{datetime.fromtimestamp(rows[0][0]):%Y-%m-%d %H:%M} ~ "
                f"{datetime.fromtimestamp(rows[-1][0]):%Y-%m-%d %H:%M}",
                "",
            ]
            fp = outdir / f"{safe}.txt"
            fp.write_text("\n".join(head + body), encoding="utf-8")
        summary.append((disp, len(rows), nemoji, nhit, rows[0][0], rows[-1][0], fp))

    out()
    out(f"{'会话':<22}{'条数':>7}{'表情':>7}{'带说明':>7}  时间跨度")
    out("-" * 78)
    for disp, n, ne, nh, f, l, fp in summary:
        out(f"{disp:<22}{n:>7}{ne:>7}{nh:>7}  "
            f"{datetime.fromtimestamp(f):%Y-%m-%d} ~ {datetime.fromtimestamp(l):%Y-%m-%d}")
    out("-" * 78)
    out(f"[✓] 共 {len(summary)} 个会话 -> {outdir}")
    return 0


# ============================================================ 子命令：all / version

def cmd_all(args):
    """一条龙。注意：--out 只作用于导出，解密产物固定在 work/<账号>。"""
    cmd_extract(argparse.Namespace(data_dir=args.data_dir, account=args.account,
                                  force=False, keyfile=None))
    cmd_refresh(argparse.Namespace(data_dir=args.data_dir, account=args.account, out=None))
    if args.no_export:
        return
    cmd_export(argparse.Namespace(data_dir=args.data_dir, account=args.account, src=None,
                                  who=args.who, out=args.out,
                                  self_name=args.self_name, format=args.format))


def cmd_version(args):
    out(f"wxbak {__version__}")
    out(f"python {sys.version.split()[0]}  ({sys.platform})")
    out(f"home   {HOME_DIR}")
    vers = wechat_versions()
    out(f"wechat {', '.join(vers) if vers else '(未检出)'}")


# ============================================================ CLI

def build_parser():
    ap = argparse.ArgumentParser(
        prog="wxbak",
        description="微信 4.x 本地聊天记录备份（Windows）",
        epilog="示例：wxbak all --who \"张三,李四\" --self-name 我 --out ./导出",
    )
    ap.add_argument("--version", action="version", version=f"wxbak {__version__}")
    ap.add_argument("--account", help="指定账号目录名或 wxid（默认自动选当前活跃账号）")
    ap.add_argument("--data-dir", help="手动指定微信数据目录（xwechat_files / 账号目录 / db_storage）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="离线自检（不碰微信，验证核心算法）")
    sub.add_parser("doctor", help="环境自检 + 分级诊断")
    sub.add_parser("accounts", help="列出所有账号")
    sub.add_parser("version", help="显示版本信息")

    p = sub.add_parser("extract", help="从内存取密钥")
    p.add_argument("--force", action="store_true", help="即使已有有效密钥也重新扫描")
    p.add_argument("--keyfile", help="从已有 all_keys.json 导入，跳过内存扫描")

    p = sub.add_parser("refresh", help="解密数据库（含 -wal）")
    p.add_argument("--out", help="输出目录（默认 work/<账号>）")

    p = sub.add_parser("list", help="列出所有会话")
    p.add_argument("--src", help="解密目录（默认 work/<账号>）")

    p = sub.add_parser("export", help="导出聊天记录")
    p.add_argument("--who", help="要导出的会话，逗号分隔（备注名/昵称/微信号/wxid），默认全部")
    p.add_argument("--src", help="解密目录")
    p.add_argument("--out", help="输出目录（默认 skill 下 导出/）")
    p.add_argument("--self-name", default="我", help="自己的显示名（默认「我」）")
    p.add_argument("--format", choices=("txt", "json"), default="txt", help="输出格式（默认 txt）")

    p = sub.add_parser("all", help="一条龙：extract → refresh → export")
    p.add_argument("--who")
    p.add_argument("--out")
    p.add_argument("--self-name", default="我")
    p.add_argument("--format", choices=("txt", "json"), default="txt")
    p.add_argument("--no-export", action="store_true")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    res = {"selftest": cmd_selftest, "doctor": cmd_doctor, "accounts": cmd_accounts,
           "extract": cmd_extract, "refresh": cmd_refresh, "list": cmd_list,
           "export": cmd_export, "all": cmd_all, "version": cmd_version}[args.cmd](args)
    return res if isinstance(res, int) else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
