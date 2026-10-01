#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prepublish_check.py — 开源发布前的隐私自检（防止把密钥 / 聊天记录 / 令牌推上去）

这个仓库天生接触高度敏感的数据：SQLCipher 密钥、解密后的数据库、聊天记录导出。
一次手滑 `git add -f` 就可能把整台电脑的隐私推到公开仓库，而 Git 历史是洗不掉的。

本脚本只读，不修改任何文件。检查三类风险：

  1. 不该被跟踪的文件   —— *.db / *.db-wal / keys/ / work/ / 导出 txt 等（含 .gitignore 是否放行异常）
  2. 文件内容里的敏感串 —— wxid、64~192 位 hex 密钥、ghp_/github_pat_ 令牌、绝对用户路径
  3. 明文凭据            —— 常见 key/token/secret 赋值形态

用法：
    python scripts/prepublish_check.py              # 检查"将要提交"的内容（暂存区）
    python scripts/prepublish_check.py --all        # 检查全部已跟踪文件（更快，适合 CI）
    python scripts/prepublish_check.py --staged     # 显式指定暂存区（默认）

退出码：0 = 通过；1 = 发现风险（可安全接入 CI / pre-push 钩子）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 规则定义

# 1) 路径级：这些文件永远不该出现在仓库里
BAD_PATH_PATTERNS = [
    (r"(^|/)keys?/", "密钥目录"),
    (r"(^|/)work/", "解密产物目录"),
    (r"\.db(-wal|-shm)?$", "SQLite 数据库"),
    (r"(^|/)all_keys\.json$", "密钥汇总文件"),
    (r"(^|/)导出/", "导出目录"),
    (r"\.(txt|sqlite|sqlite3)$", "疑似聊天记录导出（.txt 白名单见下）"),
]

# 路径级规则的显式放行
PATH_ALLOWLIST = [
    r"(^|/)requirements\.txt$",
    r"(^|/)LICENSE(\.txt)?$",
]

# 2) 内容级：出现即高危
CONTENT_PATTERNS = [
    (r"wxid_[a-z0-9]{6,}", "疑似真实 wxid"),
    (r"\b[A-Za-z0-9+/]{43}=\b", "疑似 base64 密钥"),
    (r"\b[0-9a-fA-F]{64}\b", "疑似 64 位 hex 密钥（SQLCipher raw key）"),
    (r"\b[0-9a-fA-F]{128,192}\b", "疑似长 hex 密钥"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub 访问令牌"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub 细粒度令牌"),
    (r"\bsk-[A-Za-z0-9\-_]{20,}", "OpenAI 风格 API Key"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"(?i)(key|token|secret|password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{12,}['\"]", "明文凭据赋值"),
    (r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", "绝对用户路径（泄露本机用户名）"),
    (r"/(Users|home)/[A-Za-z0-9._-]+", "绝对用户路径（POSIX）"),
]

# 3) 内容级：可疑但可能误报（例如文档里举例），单独提示
WARN_PATTERNS = [
    (r"@chatroom\b", "疑似群聊 id"),
    (r"\\xwechat_files\\|/xwechat_files/", "微信数据目录路径"),
]

# 命中"公开常量"上下文时，hex 串属于二进制特征签名而非密钥，降级为提示
PUBLIC_CONST_LINE = re.compile(r"(?i)(xor|掩码|mask|特征串|特征码|signature|magic)")

# 只扫描这些扩展名的内容（避免把二进制误当文本读）
TEXT_EXT = {".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".gitignore", ".gitattributes", ""}

MAX_BYTES = 2 * 1024 * 1024  # 单文件最多读 2MB


# ---------------------------------------------------------------- 工具函数

def git(*args: str) -> str:
    """执行 git 命令并返回 stdout（失败返回空串）。"""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
        return out.stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def is_allowed(path: str) -> bool:
    return any(re.search(p, path) for p in PATH_ALLOWLIST)


def collect_files(staged: bool) -> list[str]:
    """staged=True 取暂存区文件；否则取全部已跟踪文件。"""
    if staged:
        listing = git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
        if not listing.strip():
            # 暂存区为空时退回全部已跟踪文件，避免"假通过"
            listing = git("ls-files")
    else:
        listing = git("ls-files")
    return [p for p in listing.splitlines() if p.strip()]


def scan_content(root: Path, path: str) -> tuple[list[str], list[str]]:
    """返回 (高危命中, 提示命中)，元素形如 '第 N 行 · 规则'"""
    fp = root / path
    if not fp.is_file():
        return [], []
    if fp.suffix.lower() not in TEXT_EXT:
        return [], []
    try:
        if fp.stat().st_size > MAX_BYTES:
            return [], []
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []

    high, warn = [], []
    for i, line in enumerate(text.splitlines(), 1):
        # 形如「XOR 掩码 d2c744...」的公开二进制常量，属特征签名而非密钥
        public_const = bool(PUBLIC_CONST_LINE.search(line))
        for pat, label in CONTENT_PATTERNS:
            if re.search(pat, line):
                msg = f"{path}:{i} · {label}"
                if public_const:
                    warn.append(msg + "（同行含 XOR/掩码/特征串，判定为公开常量）")
                else:
                    high.append(msg)
        for pat, label in WARN_PATTERNS:
            if re.search(pat, line):
                warn.append(f"{path}:{i} · {label}（如为文档示例可忽略）")
    return high, warn


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="开源发布前的隐私自检")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="检查暂存区（默认）")
    g.add_argument("--all", action="store_true", help="检查全部已跟踪文件")
    args = ap.parse_args()

    staged = not args.all
    root = Path(git("rev-parse", "--show-toplevel").strip() or ".").resolve()

    if not (root / ".git").exists():
        print("[!] 当前目录不是 git 仓库，请先 git init")
        return 1

    files = collect_files(staged)
    scope = "暂存区" if staged else "全部已跟踪文件"
    print(f"[*] 仓库：{root}")
    print(f"[*] 范围：{scope}（{len(files)} 个文件）\n")

    problems: list[str] = []
    warns: list[str] = []

    # 1) 路径级
    for f in files:
        if is_allowed(f):
            continue
        for pat, label in BAD_PATH_PATTERNS:
            if re.search(pat, f, re.IGNORECASE):
                problems.append(f"[路径] {f} · {label}")
                break

    # 2) 内容级
    for f in files:
        high, warn = scan_content(root, f)
        problems.extend(f"[内容] {h}" for h in high)
        warns.extend(f"[提示] {w}" for w in warn)

    # 3) .gitignore 是否真的挡住了
    for probe in ("scripts/keys/x.json", "scripts/work/x/message_0.db", "导出/a.txt"):
        if not git("check-ignore", "--quiet", probe) and not git("check-ignore", probe):
            warns.append(f"[提示] .gitignore 未覆盖示例路径：{probe}")

    # 输出
    if problems:
        print("=" * 62)
        print(f"[X] 发现 {len(problems)} 处风险，请勿发布：")
        print("=" * 62)
        for p in problems:
            print("  " + p)
        print()
    if warns:
        print(f"[!] {len(warns)} 处提示（通常是误报，确认后可忽略）：")
        for w in warns[:30]:
            print("  " + w)
        print()

    if problems:
        print("修复建议：确认这些文件已加入 .gitignore，并用 `git rm --cached <file>` 取消跟踪。")
        print("注意：若敏感内容曾经被提交过，必须清洗 Git 历史（git filter-repo）后强推，改 .gitignore 无效。")
        return 1

    print(f"[OK] 通过：{len(files)} 个文件未发现密钥 / 聊天记录 / 令牌泄露。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
