---
name: kindle-weread-skill
description: >-
  Guides a person through preparing their own Kindle, installing the community
  jailbreak that matches the firmware, then KUAL, KOReader, and a WeRead
  plugin (weread.koplugin or miuread.koplugin). Can install the plugin over
  Wi-Fi with KOReader SSH on port 2222 when the device is already on the same
  LAN. Use when the user mentions Kindle, KOReader, KUAL, WinterBreak,
  LanguageBreak, SSH, Wi-Fi, 越狱, 微信读书, weread.koplugin, or miuread.
---

# Kindle 改装向导

帮助用户在**自己的** Kindle 上安装 KOReader 和微信读书插件。人机协作：脚本负责下载、校验和写入已挂载的 Kindle 磁盘；需要在屏幕上点按的步骤由用户完成。

工作目录是本技能根目录。所有命令都从这里运行，并加上 `--json`。日志在 stderr，结果在 stdout。

```bash
python scripts/kindle_helper.py <子命令> --json
```

Windows 用 `python`，macOS / Linux 也可以用 `bash scripts/kindle_helper.sh`。

## 必须遵守

- 一次只推进一个动作。把上一条 JSON 里的 `message` 用一两句话告诉用户，然后停下等回复。
- 不要在同一条回复里讲解后面的阶段，也不要一次性贴出全部命令。
- 只有 `scripts/kindle_helper.py` 可以写 Kindle。不要自己复制、解压或拼下载链接。
- 脚本拒绝写入系统盘。不要用 `--path` 指向用户的主目录或系统目录来“试试看”。
- 固件以 Kindle 盘内的 `system/version.txt` 为准。读不到时再问用户，不要猜。
- 架构只按下面的分界判断，不要换成别的安装包：
  - 固件 `< 5.16.3`：`legacy_softfloat`，KOReader 用 `kindlepw2`，KUAL 用 `coplate`。
  - 固件 `>= 5.16.3`：`kindlehf`，KOReader 用 `kindlehf`，KUAL 用 `hardfloat`。
- 基准机是 Kindle Paperwhite 第 7 代（KP3，序列号前缀 `G090`），固件 `5.16.2.1.1`。它走 `kindlepw2` 和 `coplate`。
- 国内或 GitHub 不稳定时，在命令后加 `--mirror https://ghfast.top/`。镜像只重试文件下载；失败时把脚本的 `error` 原样告诉用户。
- 越狱可能变砖，并会影响保修。开始前用一句话说明。LanguageBreak 还会清空设备，只有用户明确要求才使用。

## 状态

在对话里记住脚本返回的这些字段：`kindle_path`、`firmware`、`architecture`、`koreader_arch`、`kual_variant`、`jailbreak_method`、`deploy_mode`、`kindle_ip`、`stage`。USB 命令沿用 `--path`，无线命令沿用 `--ip`。

## 阶段 1：先判断 USB 还是 Wi-Fi

新对话不要先要求插线。先探测，再根据结果只说一件事：

```bash
python scripts/kindle_helper.py detect --json
python scripts/kindle_helper.py scan-wifi --json
```

`detect` 找不到磁盘时退出码是 2，这不表示失败。继续看 `scan-wifi`。

- 状态 B：`scan-wifi` 的 `kindle_ip` 有值，或 USB 上 `koreader_present` 为真且用户明确说 Kindle 已连上同一个 Wi-Fi。跳过插拔数据线，直接进入阶段 5 的无线安装。若已经扫到 IP，不要再叫用户插 USB。
- 状态 B 但没扫到端口：KOReader 已装好，只说一句，让用户打开 KOReader，进入顶部菜单 → 网络 → SSH 服务器，勾选 SSH 服务器和允许无密码登录。用户回复「SSH 已开」后重新运行 `scan-wifi`。
- 状态 A：USB 上已经找到 Kindle，但 `koreader_present` 为假，并且没有扫到 2222；或者用户明确说还没装 KOReader。告诉用户：因为亚马逊原厂限制，第一次越狱和写入 KOReader 必须用一次 USB。然后请用户打开飞行模式并连上数据线，回复「已连接」。固件 `>= 5.18.1` 时不要先开飞行模式，这种机器要先联网下载锁屏广告，走 AdBreak。
- 两个探测都没有结果，而且用户还没说明设备状态时，只问：已经装好 KOReader，并且和电脑在同一个 Wi-Fi 吗？不要同时讲解两套流程。

用户确认 USB 已连接后：

```bash
python scripts/kindle_helper.py detect --json
python scripts/kindle_helper.py clean-ota --json
```

然后只报告型号、固件和架构。`jailbroken` 且 `kual_present` 为真时，第三阶段的目标已经达成，直接进入阶段 4。已越狱但没有 KUAL 时进入阶段 3。都没有时进入阶段 2。

读不到固件时，只问型号和版本号。用户回答后，后续命令加上 `--firmware 5.16.2.1.1` 这样的值。盘内版本和用户说法冲突时，以盘内为准并告诉用户。用户已经知道 IP 时，用 `scan-wifi --ip <地址> --json`，不要再扫整段网。

## 阶段 2：越狱与 Hotfix

默认方案来自 `detect` 的 `jailbreak_method`：

- 低于 `5.18.1`：WinterBreak。`5.16.2.1.1` 也用它。
- `>= 5.18.1`：AdBreak。WinterBreak 在这个版本上不可用。
- LanguageBreak 只适用于 `<= 5.16.2.1.1`，而且会清空内容。用户点名时才加 `--method languagebreak --i-understand-this-wipes-the-kindle`。
- 用户要求不登录亚马逊商店、且固件低于 `5.16.4` 时，才改用 `--method winterbreak2`。

运行：

```bash
python scripts/kindle_helper.py install-jailbreak --json
```

WinterBreak、WinterBreak2、AdBreak 可以把返回的 `message` 整段发给用户，因为这些步骤要离开电脑连续做完。LanguageBreak 每次只说 `user_steps` 的当前一步；用户回复「可以写入」或「第二次写入」时，再运行一次同一个命令。

用户回复「已重启」并重新连上后，先看 `documents/JAILBROKEN.txt`。WinterBreak2 且 jb.sh 已写明越狱成功、正文没有要求安装 hotfix 时，系统更新已经被拦住，「更新您的 Kindle」发灰是正常的。这时不要安装 Hotfix。若根目录已经有 `Update_hotfix_universal.bin`，运行 `clean-ota --include-hotfix --json` 把它删掉，然后直接进入阶段 3。

只有越狱完成画面明确要求安装 hotfix，或用的不是 WinterBreak / WinterBreak2 时，才运行：

```bash
python scripts/kindle_helper.py install-hotfix --json
```

把 `message` 发给用户。对方回复「Hotfix 已装」后再进入阶段 3。

## 阶段 3：MRPI 与 KUAL

`kual_present` 为真时跳过本阶段。否则运行：

```bash
python scripts/kindle_helper.py install-kual --json
```

`status` 为 `already_present` 时直接进入阶段 4。否则让用户按 `message` 弹出 Kindle，在搜索栏输入 `;log mrpi`。装完并重新连接后进入阶段 4。

`;log mrpi` 没有反应时，先让用户重启 Kindle 再试一次。仍然没有反应，才运行：

```bash
python scripts/kindle_helper.py install-kual --launcher peki --json
```

然后让用户在书库里点开 KUAL。

## 阶段 4：KOReader

不要让用户手动拖文件夹。按已锁定的架构运行其中一个：

```bash
python scripts/kindle_helper.py install-koreader --arch kindlepw2 --json
python scripts/kindle_helper.py install-koreader --arch kindlehf --json
```

成功后把 JSON 里的 `message` 发给用户。USB 安装会预置 KOReader 的 SSH：端口 2222、随 KOReader 启动、允许无密码登录。这次数据线还插着，插件继续走 USB，不要中途改成无线。

## 阶段 5：微信读书插件

先只问这一句：

> 要装哪一个？A：`weread.koplugin`，轻量，用微信读书官方 Skill 扫码。B：觅阅 `miuread.koplugin`，功能更多，包含排版和美化。也可以两个都装。

数据线还连着时：

```bash
python scripts/kindle_helper.py install-plugin --choice weread --json
python scripts/kindle_helper.py install-plugin --choice miuread --json
python scripts/kindle_helper.py install-plugin --choice both --json
python scripts/kindle_helper.py finalize --json
```

状态 B 用无线命令，不要要求插 USB：

```bash
python scripts/kindle_helper.py wifi-install-plugin --ip <Kindle_IP> --choice weread --json
```

`--choice` 同样可以是 `miuread` 或 `both`。脚本只把目录传到 `root@<IP>:2222` 的 `/mnt/us/koreader/plugins/`，并在设备上执行 `sync`。端口没开时，把返回的 `message` 原样告诉用户，等对方回复「SSH 已开」后再运行同一条命令。成功后把 `message` 发给用户：在 KOReader 里完全退出并重新打开，然后扫码。不要再让用户弹出 USB。

脚本会把目录名固定成 `weread.koplugin` 或 `miuread.koplugin`，并要求里面有 `main.lua`。无线传输只接受局域网私有地址。

## 异常时只说原因和下一步

- 找不到 Kindle：请确认 USB 已连接，必要时在资源管理器中显示隐藏文件，然后回复「已连接」。
- 找到多个盘：请用户看盘符，下一次命令加 `--path`。
- 剩余空间不足：先让用户删除不需要的书。开着 Wi-Fi 时不要为了腾空间而删除文件，避免系统趁机下载更新。
- 下载失败：重试一次；再失败就加上 `--mirror` 后重试同一条子命令。
- 架构冲突：不要加 `--force`，除非用户看过固件版本后明确要求。
- AdBreak 找不到 `.assets`：让用户先联网下载广告，打开 View all ads，再开飞行模式并重连。
- 需要还原 AdBreak 改过的广告文件：`python scripts/kindle_helper.py restore-assets --json`。
- 扫不到 2222：只让用户在 KOReader 里打开 SSH 和无密码登录，不要同时改讲 USB 越狱。
- 找到多台设备：把 IP 列出来，等用户选定后再传 `--ip`。
- 无线登录失败：不要换端口或猜测密码。仍旧只让用户勾选允许无密码登录。

## 命令

| 子命令 | 作用 |
| --- | --- |
| `detect` | 找盘、剩余空间、固件、序列号前缀、越狱标记 |
| `clean-ota` | 删除根目录的 `update.bin.tmp.partial` 等升级残留 |
| `install-jailbreak` | 下载并写入越狱包，Hotfix 先放进缓存 |
| `install-hotfix` | 把一个 Hotfix `.bin` 写到根目录 |
| `install-kual` | 写入 MRPI，并把对应 KUAL 的 install `.bin` 放进 `mrpackages` |
| `install-koreader --arch` | 下载匹配架构的最新 KOReader 并合并到根目录 |
| `install-plugin --choice` | 经 USB 安装 `weread`、`miuread` 或 `both` |
| `scan-wifi` | 扫描本机所在局域网里开放 2222 的设备，也可用 `--ip` |
| `wifi-install-plugin --ip --choice` | 用 SSH/SFTP 把插件传到 `/mnt/us/koreader/plugins/` 并执行 `sync` |
| `finalize` | 同步磁盘并删除下载缓存 |
| `self-check` | 离线检查路由和压缩包安全逻辑 |
