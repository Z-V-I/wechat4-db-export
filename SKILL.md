---
name: wechat4-db-export
description: 在 Windows 上解密微信 4.x（含 4.1+）本地数据库，把聊天记录导出成按发言人/时间戳拆分的 txt 或 json，支持增量刷新（自动合并 -wal 里的最新消息）。当用户要求「导出微信聊天记录」「解密微信数据库」「备份/更新微信聊天数据」「按人物拆分聊天」「看微信表情包是什么」「查会话有多少条」时使用。关键词：微信4.1、Weixin.exe、xwechat_files、db_storage、SQLCipher、WCDB、Config.Cipher、-wal、微信聊天记录导出、chatlog、wxbak。
agent_created: true
---

# 微信 4.x 本地数据库解密与聊天记录导出

在 Windows 上把微信 4.x（已在 4.1.15.13 实测）的本地加密数据库解密，导出可读聊天记录。

**这是本地取证**：数据本来就在用户硬盘上、密钥本来就在用户自己的进程内存里，全程不与微信服务器交互、不绕过登录、不修改微信数据。
**只处理用户本人这台电脑上、用户本人账号的数据；不要用于他人设备。**

## 给智能体的执行须知

- **不要**手工拼命令去解数据库 —— 统一走 `scripts/wxbak.py`，它已内置目录自动发现、密钥缓存、WAL 合并、自检与分级诊断。
- 用户说「更新/刷新一下聊天记录」= 重跑 `refresh` + `export`（密钥会复用，不必重扫内存）。
- 用户没指定导出目录时，**先问**或默认导出到桌面新建的目录；不要写到 skill 目录里（那是工作区，会被 git 忽略）。
- 用户只说「导出聊天记录」时，先跑 `list` 把会话清单和条数给他看，再让他挑。
- 导出前如果 `doctor` 提示 WAL 很大或数据库刚被写入，**提醒用户等几秒再抓**（否则会漏最新消息）。

## 快速开始

统一入口 `scripts/wxbak.py`。零人工适配：自动发现账号目录、自动识别活跃账号、密钥按账号分开存、运行前自检、失败时分级诊断。

```bash
cd <本 skill>/scripts

# 0) 离线自检：不碰微信，验证 解密 / WAL / 渲染 三条核心链路（换机器后先跑这个）
python wxbak.py selftest

# 1) 环境自检：依赖 / 权限 / 微信是否在跑 / 微信版本 / 数据是否刚落盘
python wxbak.py doctor

# 2) 一条龙（首次自动取密钥；之后复用，不再扫内存）
python wxbak.py all --who "备注名1,备注名2" --self-name 我 --out "D:\导出"

# 也可以分步：
python wxbak.py accounts              # 本机有哪些微信账号
python wxbak.py extract               # 取密钥（已有有效密钥则自动跳过）
python wxbak.py refresh               # 解密（含 -wal）
python wxbak.py list                  # 列会话 + 条数，挑要导的
python wxbak.py export --who "张三" --self-name 我 --out "D:\导出"
python wxbak.py version               # 版本 / 环境 / 微信版本
```

| 参数 | 说明 |
|---|---|
| `--account` | 多账号时指定（目录名或 wxid）。不指定则自动选 mtime 最新的活跃账号 |
| `--data-dir` | 手动指定数据目录（`xwechat_files` / 账号目录 / `db_storage` 都认）。自动发现失败时用它 |
| `--who` | 备注名 / 昵称 / **微信号** / wxid 都认；省略则导出全部会话 |
| `--format` | `txt`（默认）或 `json` |
| `--self-name` | 自己的显示名，默认「我」 |

产物位置：密钥 `scripts/keys/<账号目录名>.json`，解密库 `scripts/work/<账号目录名>/`，导出 `--out` 指定。
可用环境变量 `WXBAK_HOME` 把这两者重定位到别处。

> 早期那些把账号目录、wxid、人员名单、输出路径**写死在代码里**的一次性脚本已从本包移除
> （它们会泄露使用者自己的账号信息，不适合随包分发）。全部能力都已并入 `wxbak.py`。
> `scripts/wcdb_key.py` 仍需保留，`wxbak.py extract` 会调用它。

## 依赖

- Windows（依赖 Windows 进程内存布局；Linux/macOS 见「移植到其他平台」）
- Python ≥ 3.9，第三方包见 `requirements.txt`：`pycryptodome`、`zstandard`
  （不要 pip 全局装；建隔离 venv：`python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`）
- **管理员权限** —— 读进程内存必需，UAC 弹窗躲不掉
- 微信 4.1+ 已启动并已登录

## 安装到别的智能体

本目录就是标准 Skill 包（`SKILL.md` + `scripts/`）。复制/克隆到宿主的技能目录即可：

| 宿主 | 位置 |
|---|---|
| WorkBuddy / CodeBuddy | `~/.workbuddy/skills/wechat4-db-export/` |
| Claude Code | `~/.claude/skills/wechat4-db-export/` |
| 其他 Agent Skills 宿主 | `<skills 目录>/wechat4-db-export/` |

面向人类的介绍、安装与法律说明在 `README.md`。

---

## 关键事实（踩过的坑，务必照做）

### 1. 数据目录变了，而且每个人不一样

微信 4.x 不再是 `Documents\WeChat Files`，而是：

```
<盘>:\Documents\xwechat_files\<wxid>_<后缀>\db_storage\
```

数据根目录**由用户安装时选择**，可能在任何盘。**权威来源是微信自己的配置**：

```
%APPDATA%\Tencent\xwechat\config\*.ini     ← 内容就是数据根目录，例如 "D:\Documents"
```

`wxbak.py` 按「ini → 常见位置 → 全盘符扫描」的顺序发现，都失败时用 `--data-dir` 兜底。

聊天库在 `db_storage\message\message_*.db`（**分片，数量不固定**，同一会话可能横跨多个库），因此必须 glob 发现而不是写死 0/1/2。

### 2. 取密钥：4.0 和 4.1 完全不同 ⚠️

- 网上多数工具（chatlog v0.0.31、各类 `getkey`）针对 **4.0**：内存里缓存的是 PBKDF2 **之后**的原始 32 字节密钥。
- **4.1 变了**：内存里缓存的是**派生前的 passphrase**，或把派生密钥包在 `Config.Cipher` 对象里。用 4.0 的特征码扫 **必然失败**（会扫出一堆 32 字节十六进制字符串，个个都不对）。
- **4.1 正确做法**（`scripts/wcdb_key.py`，移植自 wcdb-key-tool）：

| 步骤 | 值 |
|---|---|
| 特征串 | `com.Tencent.WCDB.Config.Cipher` |
| XOR 掩码 | `d2c7442458020000004889442450488b450048844c2448488944254048584c24` |
| 命中处 | `node = hit - 0x10`，`config_ptr = u64(node + 0x28)` |
| 读对象 | `config_ptr + 0x88` 起 0x28 字节 → `data_ptr=obj[8:16]`，`data_len=obj[16:24]` |
| 解码 | blob 用 XOR 掩码循环解码，再正则 `x'([0-9a-fA-F]{64,192})'` 抠密钥 |

- **4.1 是「原始密钥模式」**：取到的 32 字节**直接当 AES 密钥**，**不要**再跑 256000 次 PBKDF2（跑错就全解不开）。

```bash
python scripts/wcdb_key.py extract --db-dir "<db_storage>" --output all_keys.json
```

### 3. 密钥「按安装固定」，不用反复扫内存 ✅

`keys/<账号>.json` 里的 `enc_key` **跨微信重启、跨数据更新都有效**。
所以：**第一次之后不用再扫内存**，直接用旧密钥解密；有效性用 page-1 HMAC 校验（见下）。
只有**重装微信 / 换账号**时才需要 `extract --force`。
`doctor` 的第 [7] 项会逐个库报告有效/失效。

### 4. SQLCipher 4 页格式

页大小 4096，AES-256-CBC，预留 80 字节 = `IV(16) + HMAC(64)`，`salt = 第 1 页前 16 字节`。

- 页布局：`[密文][IV(16)][HMAC(64)]`
- **第 1 页特殊**：前 16 字节是明文盐，密文从偏移 **16** 开始；其他页密文从偏移 **0** 开始
- `mac_salt = salt XOR 0x3A`，`mac_key = PBKDF2-SHA512(enc_key, mac_salt, 2, 32)`
- 校验：`HMAC-SHA512(mac_key, 页[16:4016] + LE32(1))` == `页[4032:4096]`
- 重建明文页：`b"SQLite format 3\x00" + 解密结果 + b"\x00"*80`（第 1 页），或 `解密结果 + b"\x00"*80`（其他页）
- 因此**每页只有前 4016 字节是有效内容**，尾部 80 字节解密后置零

### 5. 必须解密 `-wal`，否则丢掉所有新消息 ⚠️⚠️

微信长期开着，**平日的增量消息大多只在 `-wal` 里，没有合并进主库**（实测主库文件几天没变，而 wal 有几 MB / 上千帧）。
**只解主库 = 新消息全丢。**

WAL 解密格式（实测通过）：

- 前 **32 字节** WAL 头、每帧 **24 字节**帧头 **都是明文**（标准 SQLite WAL magic `377f0682` / `377f0683`），页号在帧头前 4 字节（大端 `>I`）
- 帧内 4096 字节页数据仍按上面的 SQLCipher 规则解（第 1 页跳过 16 字节，其他页不跳）
- 重建输出：`32字节原样头 + 每帧(24字节原样帧头 + 4096字节解出的明文页)`
- 校验和、盐都原样保留 → SQLite 打开时会**自动 replay WAL**，最新数据就出来了
- **不要复制 `-shm`**，让 SQLite 自己重建索引
- 落地方式：`x.db` → `out/x.db`，`x.db-wal` → `out/x.db-wal`，同目录 `sqlite3` 打开即可

> 为什么这样能成立：SQLCipher 在读取 WAL 帧时**先解密再校验校验和**，所以帧头的校验和对应的是明文页面。这也是为什么离线自检里无法用标准 `sqlite3` 去打开自造的加密 WAL —— 标准库不会解密，只能做字节级往返比对。

### 6. 刷新前先看源库时间 ⚠️

微信**正在使用时数据可能还没落盘**。实测：用户以为「已经更新了」，但第一次快照只到 9-28；
几分钟后 `db_storage\message\message_0.db` 从 479KB 涨到 **1036KB**，重新解密就出现了 9-30 / 10-1 的记录。

**刷新前先看 `db_storage\message\message_*.db` 的 mtime 和大小**（`doctor` 第 [6] 项已自动做）：

- mtime 是「刚刚」（< 2 分钟）→ 数据在写入，等一会儿或直接重跑一次
- 解密完成后，把快照 size 与源库 size 再对一次，确认不是半写状态（`refresh` 已自动校验并警告）

### 7. 读消息表

- 表名 = `Msg_` + `md5(对方 wxid)`
- 发送者 = **权威字段**，不要靠语气猜：

```sql
SELECT m.sort_seq, n.user_name, m.create_time, m.local_type,
       m.message_content, m.WCDB_CT_message_content
FROM "Msg_<md5>" m LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid
```

- `user_name == 自己的 wxid` → 本人；`NULL/''/'0'` → 系统
- **每个库的 `Name2Id` 是独立的**，不能跨库 join，必须按库分别查
- `contact.db` 的 `contact` 表提供显示名：`remark`（备注）/ `nick_name`（昵称）/ `alias`（**微信号**）—— 注意别把「昵称」当「微信号」
- 群聊成员名同样来自 `contact`；群列表在 `contact.db` 的 `chat_room`（`username`）与 `chatroom_member`（`room_id`/`member_id`）

### 8. 两个必踩的坑

- `WCDB_CT_message_content = 4` → 内容是 **zstd 压缩**，先 `zstandard.decompress` 再解码
- 正文里的空格被存成 XML 转义 `&#x20;` → **必须 `html.unescape`**，否则比对/判断会错

### 9. local_type 语义

`1` 文本｜`3` 图片｜`34` 语音｜`42` 名片｜`43` 视频｜`47` 表情｜`48` 位置｜`49`/`244813135921` 引用/App 消息（`<appmsg>`，正文在 `<title>`）｜`50/64/66` 通话｜`10000` 系统消息｜`21474836529`/`34359738417`/`81604378673`/`8589934592049`/`8594229559345` 链接。

**系统消息（撤回等）解析坑**：`local_type=10000` 的撤回消息内容是
`<sysmsg type="revokemsg"><revokemsg><content>"X" 撤回了一条消息</content><revoketime>0</revoketime></revokemsg></sysmsg>`
直接 strip 标签会多出 `<revoketime>` 的 `0`。正确顺序：`<replacemsg>`（含 CDATA）→ `<content>` → 最后才 strip。

### 10. 富媒体渲染：引用消息与转义嵌套 ⚠️

不处理这一层，导出文件里会混进**成吨的原始 XML**（实测 1000+ 行）。

**A. 引用回复 `local_type=244813135921`** 结构是：

```xml
<msg><appmsg>
  <title>自己回复的话</title>
  <refermsg>
    <fromusr>wxid_xxxx</fromusr>
    <displayname>昵称</displayname>
    <content>被引用的原话</content>   ← 也可能是嵌套的富媒体 XML
  </refermsg>
</appmsg></msg>
```

→ 渲染为：`自己回复的话  〔回复 显示名：被引用的原话〕`
显示名优先用 `fromusr` 去 `contact.db` 查备注/昵称，查不到才退回 `<displayname>`（那是原始昵称，通常是乱码一样的符号，很难看）。

**B. 转义嵌套的坑（关键）**：`<refermsg><content>` 里若嵌套了富媒体，内容是**被 XML 转义的**（`&lt;msg&gt;&lt;appmsg&gt;...`）。
**处理顺序必须是「先 `html.unescape` 还原实体，再剥标签，且要循环到稳定」**——
如果先剥标签，`&lt;` 不是 `<`，剥不掉；之后 unescape 又把它们还原成真标签，于是原始 XML 就漏进正文了。

```python
def _strip(x):
    x = CDATA_RE.sub(r"\1", x)
    for _ in range(4):
        y = html.unescape(re.sub(r"<[^>]+>", "", x))
        if y == x:
            break
        x = y
    return x.strip()
```

被引用内容是富媒体时，优先取它的 `<title>`。

**C. CDATA 包裹**：转账（`8589934592049`）、红包（`8594229559345`）等的 title/des 形如
`<title><![CDATA[微信转账]]></title>` —— **必须剥掉 CDATA 壳**，否则正文出现 `<![CDATA[微信转账]]>`。

**D. 其余富媒体**统一走 `title + des`：转账/红包/合并转发/公众号文章/小程序都是这个结构。
`34359738417`（`<type>8</type>` + `appattach/fileext`）是**文件**，单列 `[文件]`。

### 11. 表情包

- 消息里存的**不是图片**，是一条 XML：`<emoji fromusername tousername type md5 len productid cdnurl aeskey width height attachedtext />`
- 真正的 GIF 文件在 `business\emoticon\{Persist,Thumb}`、`cache\<年-月>\Emoticon\`，**以 md5 命名**，可与消息里的 `md5` 一一对上（实测覆盖 100%）
- **文件本身是加密的**（长度是 16 的倍数、无 ECB 重复块 → CBC），且 **`emoticon.db` 里的 `aes_key` 不是它的密钥**（那是 CDN 下载用的；实测同一 16 字节文件头的 56 个文件的 aes_key 有 **27 种不同**）。本地文件密钥疑似全安装统一的固定值，尚未破解；图片/表情密钥只在「查看」瞬间才进内存（扫过 1.4GB 进程内存，0 命中）
- ✅ **能白拿的**：`emoticon.db` 的 `kStoreEmoticonCaptionsTable` / `kNonStoreEmoticonTable` 存了表情的**文字说明**（如"鼠了算了""假装哭"），但**只覆盖本机装过的表情包**，对方发来的查不到（实测覆盖率约 15%）。渲染成 `[表情#<md5前8位> 说明：xxx]`
- 其他表：`kStoreEmoticonPackageTable`（包名）、`kStoreEmoticonFilesTable`（md5→包）
- 取字段要**按列名而非列序**（不同版本列序会变）

---

## 失效诊断（「难维护」到底难在哪，怎么处理）

这个 skill 有一个**结构性弱点**：取密钥依赖的内存结构偏移是**针对具体微信版本**的。
微信升级次版本（如 4.1.15 → 4.2.x）就可能改变结构 → 扫描 0 命中。
**但失效不是灾难，因为有明确的分级排查路径。** 遇到「取密钥失败」按顺序走（`extract` 失败时会自动打印这份诊断）：

| # | 症状 | 判断方法 | 处理 |
|---|---|---|---|
| ① | 微信没运行 / 没登录 | `doctor` 第 [3] 项 | 启动微信并登录，密钥只在登录后进内存 |
| ② | 不是管理员 | `doctor` 第 [2] 项 | 以管理员身份重新运行 |
| ③ | 微信升级了（**最可能**） | `doctor` 第 [4] 项对比本机版本与已验证列表 | 见下方「版本漂移修复」 |
| ④ | 内存里 Cipher 对象还没构造 | 微信刚启动 / 刚更新完，还没点开任何聊天 | 在微信里点开任意聊天窗口，再重试 |
| ⑤ | 密钥只是局部失效 | `doctor` 第 [7] 项显示「N 个有效 / M 个失效」 | 仅个别无关库失效可直接忽略；**message_\*.db 大面积失效**才重取 |

### 版本漂移修复（只在第 ③ 项命中时做）

需要重新逆向的只有 `wcdb_key.py` 里这几个常量：

| 常量 | 当前值（4.1.11/4.1.15） | 含义 |
|---|---|---|
| `WINDOWS_CONFIG_CIPHER_NAME` | `com.Tencent.WCDB.Config.Cipher` | 内存特征串（一般不变） |
| `WINDOWS_CONFIG_XOR_MASK` | `d2c7442458020000004889442450488b450048844c2448488944254048584c24` | 定位结构用的 XOR 掩码 |
| `node` 取 `config_ptr` 的偏移 | `+0x28` | 命中处往回 `0x10` 是节点，节点 `+0x28` 是对象指针 |
| 读对象的偏移 / 长度 | `config_ptr + 0x88`，`0x28` 字节 | `data_ptr=obj[8:16]`、`data_len=obj[16:24]` |

修复思路：参考开源项目 **wcdb-key-tool**（GitHub）的最新实现更新以上常量；
或用 x64dbg 在 `Weixin.exe` 里对配置文件名的字符串引用下断点，反推新的结构偏移。
**其余全部逻辑（SQLCipher 解密、WAL、消息解析、导出）与版本无关，不需要动** ——
`python wxbak.py selftest` 就是用来在改完常量后快速确认这部分没被改坏的。

## 移植到其他平台

平台相关部分被刻意隔离在两处：

1. **取密钥**（`wcdb_key.py`）：需针对 macOS/Linux 版微信重新逆向内存结构
2. **进程枚举 / 内存读取**（`_list_wechat_pids`、`ReadProcessMemory`，仅 win32）

`dec_db` / `dec_wal` / `render` / 导出与平台无关，可直接复用；无微信环境下 `selftest` 就能验证这部分。

## 项目结构

```
SKILL.md                  本文件（智能体入口）
README.md                 面向人类的介绍与安装说明
LICENSE                   MIT
requirements.txt          运行依赖
.gitignore                屏蔽 keys/ work/ 等隐私产物
.gitattributes            统一换行（LF），二进制文件不做转换
scripts/
  wxbak.py                统一入口：selftest/doctor/accounts/extract/refresh/list/export/all/version
  wcdb_key.py             取密钥（4.1 Config.Cipher 路线），移植自 wcdb-key-tool
  prepublish_check.py     开源发布前的隐私自检（拦截密钥/聊天记录/令牌/本机路径）
  keys/                   密钥（git 忽略；可用 WXBAK_HOME 重定位）
  work/                   解密产物（git 忽略）
```

## 开源 / 二次开发前必做

本仓库天生接触敏感数据。**提交或发 PR 前先跑隐私自检**（退出码 0 通过 / 1 有风险）：

```bash
python scripts/prepublish_check.py          # 检查暂存区（默认）
python scripts/prepublish_check.py --all    # 检查全部已跟踪文件（适合 CI）
```

它拦截三类风险：不该被跟踪的文件（`*.db` / `keys/` / `work/` / 导出 txt）、文件内容里的敏感串（wxid、hex 密钥、`ghp_` 令牌、本机绝对路径）、明文凭据赋值。
注意「XOR 掩码 / 特征串」这类公开二进制常量会被降级为提示，不算风险。

> 若敏感内容**曾经被提交过**，改 `.gitignore` 无效 —— 必须用 `git filter-repo` 清洗历史后强推。

## 边界 & 纪律

- 只读用户自己的设备与账号；不绕过登录、不攻击服务器、不改协议、不修改微信数据。
- 能力等价于「解密任意一台电脑上任意已登录账号的微信库」——**永远不要用于他人设备**。
  在中国大陆，未经授权读取他人数据可能触犯《刑法》第 285 条与《个人信息保护法》。
- 导出文件含高度隐私，默认写到用户指定目录，不要外传、不要提交进 git（`.gitignore` 已屏蔽）。
- 微信运行中也能解密（密钥已在手时），但为拿到最新数据要**一并解 `-wal`**，并注意「刚写入可能未落盘」（见第 6 条）。
