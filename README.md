# wxbak · 微信 4.x 聊天记录本地导出

> 把你的微信聊天记录从本地数据库里解密、导出成可读的 `txt` / `json`，按发言人和时间戳逐条拆分。
>
> **纯本地取证**：数据在你自己硬盘上、密钥在你自己进程内存里。全程不连微信服务器、不绕过登录、不改协议。
>
> Decrypt and export your own WeChat 4.x chat history from the local encrypted SQLite databases. Local forensics only — no server interaction, no login bypass.

---

## 它解决什么问题

微信 4.x 把聊天记录存成本地加密数据库，密钥**不落盘**、版本一变就取不到。市面上的工具大多只支持微信 4.0，在 4.1 上会静默失败。

`wxbak` 面向的是**微信 4.1+**，并且把"换台电脑就得改代码"的部分全部做成了自动发现：

| 痛点 | wxbak 的做法 |
|---|---|
| 数据目录每个人都不一样 | 读微信官方配置 ini + 全盘符回退扫描，也可 `--data-dir` 手动指定 |
| 消息库分片数量不一致 | 自动发现 `message_*.db`，不写死 0/1/2 |
| 换人/换账号要改代码 | 密钥与解密产物按账号分开存放，多账号共存 |
| 新消息神秘消失 | 一并解密 `-wal`（微信的增量几乎全在 WAL 里） |
| 出错了不知道为啥 | `doctor` 自检 + `extract` 失败分级诊断 |
| 改坏了不知道 | `selftest` 离线自检，不碰微信也能验证核心算法 |

---

## 安装

### 方式一：作为「智能体技能」安装（推荐）

本目录就是一个标准 Skill 包（`SKILL.md` + `scripts/`）。把它放到你的智能体技能目录即可：

| 智能体 | 放置位置 |
|---|---|
| WorkBuddy / CodeBuddy | `~/.workbuddy/skills/wechat4-db-export/` |
| Claude Code | `~/.claude/skills/wechat4-db-export/` |
| 其他支持 Agent Skills 的宿主 | `<你的 skills 目录>/wechat4-db-export/` |

```bash
git clone https://github.com/Z-V-I/wechat4-db-export.git ~/.workbuddy/skills/wechat4-db-export
```

装好之后，直接对智能体说「导出我和某某的微信聊天记录」即可 —— `SKILL.md` 里写清了完整流程、每一个坑和失败时的排查路径。

### 方式二：当作命令行工具用

```bash
git clone https://github.com/Z-V-I/wechat4-db-export.git wxbak && cd wxbak
python -m pip install -r requirements.txt

python scripts/wxbak.py selftest   # 先自检，不碰微信
python scripts/wxbak.py doctor     # 再看环境
```

**要求**

- Windows（依赖 Windows 进程内存布局；Linux/macOS 见下方「其他平台」）
- Python ≥ 3.9
- **管理员权限**（读取微信进程内存需要）
- 微信 4.1+ 已启动并已登录

---

## 使用

```bash
# 0) 离线自检：验证解密算法、WAL 重组、消息渲染是否正常
python scripts/wxbak.py selftest

# 1) 环境自检：缺依赖 / 没开微信 / 没提权 / 微信版本变了 / 数据没落盘，都会在这里暴露
python scripts/wxbak.py doctor

# 2) 一条龙（首次自动取密钥，之后复用；--who 支持备注名/昵称/微信号/wxid）
python scripts/wxbak.py all --who "张三,李四" --self-name 我 --out "D:\导出"

# 3) 也可以分步
python scripts/wxbak.py accounts    # 本机有哪些微信账号
python scripts/wxbak.py list        # 所有会话及条数（先看看要导哪些）
python scripts/wxbak.py extract     # 取密钥（已有有效密钥会自动跳过）
python scripts/wxbak.py refresh     # 解密数据库（含 -wal）
python scripts/wxbak.py export --who "张三" --self-name 我 --out "D:\导出" --format txt
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--account` | 多账号时指定账号（目录名或 wxid）；不指定则自动选当前活跃账号 |
| `--data-dir` | 手动指定微信数据目录（`xwechat_files` / 账号目录 / `db_storage` 都行） |
| `--who` | 要导出的会话，逗号分隔；备注名、昵称、微信号、wxid 都认 |
| `--self-name` | 你自己的显示名，默认「我」 |
| `--format` | `txt`（默认）或 `json` |
| `--out` | 导出目录 |

### 输出长什么样

```
# 与 张三 的聊天记录
# 备注：张三    昵称：San    微信号：san_2024
# wxid：wxid_xxxx    类型：单聊    消息数：2180    时间：2025-09-14 10:02 ~ 2026-09-30 23:57

[2026-09-30 23:57] 张三：OK，我跑路了
[2026-09-30 23:58] 我：[表情#0aefaa01 说明：鼠了算了]
[2026-09-30 23:59] 张三：引用的原话  〔回复 我：被引用的那条消息〕
[2026-10-01 00:01] 张三：[图片]
[2026-10-01 00:02] 系统："张三" 撤回了一条消息
```

- 发言人来自数据库权威字段（`Name2Id.real_sender_id`），**不是靠语气猜的**
- 群聊会自动解析成员显示名，并在头部标注「类型：群聊」
- 引用回复、转账、红包、合并转发、文件、位置等富媒体都会被还原成可读文本，不会漏出原始 XML

---

## 它是怎么做到的

四个阶段，全部离线：

```
① 定位数据目录   读微信官方配置 ini + 盘符扫描
        ↓
② 从内存取密钥   搜 com.Tencent.WCDB.Config.Cipher → 解出 32 字节密钥
        ↓
③ 解密数据库     SQLCipher 4（页 4096 / AES-256-CBC / 预留 80 字节），并一并解 -wal
        ↓
④ 解析并导出     Name2Id 判发言人 → zstd 解压 → XML 渲染 → 按时间排序输出
```

**核心难点是 ②。** 微信 4.0 与 4.1 的内存结构完全不同：

- 4.0：内存里直接缓存 PBKDF2 **之后**的原始密钥（多数现成工具走这条路）
- 4.1：缓存的是**派生前**的凭据，或把密钥包在 `Config.Cipher` 对象里 —— 用 4.0 的特征码去扫**必然失败**

`wxbak` 走的是 4.1 路线，并且**取到的 32 字节直接当 AES 密钥**（不再跑 256000 次 PBKDF2 —— 跑错就全解不开）。

**第二个坑是 WAL。** 微信常驻后台，日常增量大多只写在 `message_*.db-wal` 里、没合并进主库。只解主库 = 这几天的新消息全部丢失。`wxbak refresh` 会一并解密 WAL，并保持格式让 SQLite 自动 replay。

完整的踩坑清单（含 SQLCipher 页布局、WAL 帧格式、`&#x20;` 转义、引用消息的嵌套转义顺序等）在 **[SKILL.md](SKILL.md)**。

---

## 常见问题

**Q：取密钥失败怎么办？**
按顺序排查：① 微信没开/没登录 → ② 没用管理员运行 → ③ **微信升级了**（最常见）→ ④ 微信刚启动、Cipher 对象还没构造（点开任意聊天窗口再试）。`extract` 失败时会自动打印这份分级诊断。

**Q：微信升级后还能用吗？**
大概率能。需要改的只有 `wcdb_key.py` 里 4 个与内存结构相关的常量；解密、WAL、解析、导出全部逻辑与版本无关。详见 SKILL.md「版本漂移修复」。

**Q：为什么表情包只能看到编号？**
消息里存的不是图片而是一条 XML（含 md5、包 id、CDN 地址）。真正的 GIF 在 `business\emoticon\` 下按 md5 命名、但文件本身是加密的。能免费拿到的是 `emoticon.db` 里的**文字说明**，且只覆盖你本机装过的表情包（实测约 15% 命中）。

**Q：密钥会过期吗？**
不会。密钥**按安装固定**，跨微信重启、跨数据更新都有效。只有重装微信或换账号才需要重新取。`doctor` 会用 page-1 HMAC 逐个校验。

**Q：会不会被微信封号？**
不会。本工具**不连接微信服务器**、不发送任何请求、不修改微信数据，只读你硬盘上的文件和进程内存。

**Q：能导出公众号消息吗？**
公众号走 `biz_message_*.db`，结构不同，当前版本未覆盖。欢迎 PR。

---

## 其他平台

代码把平台相关的部分集中隔离了，移植主要需要重写两处：

1. **取密钥**（`wcdb_key.py`）：需要针对 macOS/Linux 版微信重新逆向内存结构
2. **进程枚举与内存读取**（`_list_wechat_pids` / `ReadProcessMemory`）

SQLCipher 解密、WAL 重组、消息解析、导出这些**与平台无关**，可以直接复用。跑 `selftest` 就能在无微信的环境下确认这部分没坏。

---

## 二次开发 / 发布前自检

这个仓库天生接触敏感数据（密钥、解密后的库、聊天记录）。**发布或 PR 之前务必跑一次隐私自检**：

```bash
python scripts/prepublish_check.py          # 检查将要提交的内容（暂存区）
python scripts/prepublish_check.py --all    # 检查全部已跟踪文件（适合接 CI）
```

它会拦截三类风险：不该被跟踪的文件（`*.db` / `keys/` / `work/` / 导出 txt）、文件内容里的敏感串（wxid、hex 密钥、`ghp_` 令牌、本机绝对路径）、明文凭据赋值。退出码 `0` 通过、`1` 有风险，可直接用作 pre-push 钩子：

```bash
printf '#!/bin/sh\npython scripts/prepublish_check.py --all || exit 1\n' > .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

> 若敏感内容**曾经被提交过**，改 `.gitignore` 是没用的 —— 必须用 `git filter-repo` 清洗历史后强推。

---

## 法律与伦理（请务必阅读）

这个工具的能力边界必须说清楚：**等价于「解密任意一台电脑上、任意已登录账号的微信数据库」**。

- ✅ **用它读你自己的数据** —— 就像用钥匙打开自家的保险柜，完全正当
- ❌ **用它读别人的数据** —— 在形态上就是隐私窃取工具。在中国大陆可能触犯《刑法》第 285 条（非法获取计算机信息系统数据罪）与《个人信息保护法》

本项目的设计约束：**只读**、**不绕过登录**、**不与服务器交互**、**不修改任何微信数据**。请勿用它做任何超出「读取自己数据」范围的事。导出的文件含高度隐私，请自行妥善保管。

---

## English summary

`wxbak` decrypts and exports **your own** WeChat 4.x chat history on Windows. It targets WeChat **4.1+** (most existing tools only handle 4.0 and fail silently on 4.1).

- **Auto-discovery**: data directories, message shards, and accounts are discovered automatically — no per-machine hardcoding
- **Key extraction**: scans the `com.Tencent.WCDB.Config.Cipher` structure in `Weixin.exe` memory (the 4.1 layout)
- **Full-fidelity**: decrypts main DBs **and** `-wal` files, so recent messages aren't lost
- **Authoritative speakers**: resolved via `Name2Id.real_sender_id`, never guessed
- **Self-diagnosing**: `selftest` (offline), `doctor` (environment), tiered diagnostics on key-extraction failure

Requirements: Windows, Python ≥ 3.9, admin rights, WeChat 4.1+ logged in. Local forensics only — no server interaction, no login bypass.

Licensed under the [MIT License](LICENSE).
