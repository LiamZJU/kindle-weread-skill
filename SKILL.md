---
name: kindle-weread-skill
description: >-
  Guides a person through preparing their own Kindle, installing the community
  jailbreak that matches the firmware, then KUAL, KOReader, the WeRead plugin,
  and Qiaomu SimpleUI as the home screen. Once the Kindle is connected, the
  computer-side install runs straight through. The first KOReader launch opens
  SimpleUI and asks the user to scan a WeChat QR code on the Kindle. An
  existing SimpleUI install is left in place. Wi-Fi install works when the
  user temporarily turns on KOReader SSH on port 2222; the install does not
  leave SSH auto-starting. USB install-plugin and install-simpleui also clear
  an existing SSH_autostart. A wireless install leaves the running settings
  file alone and wekindle.koplugin clears autostart on the next launch. The
  user turns SSH off afterwards. Use when the
  user mentions Kindle, KOReader, KUAL, WinterBreak, LanguageBreak, SSH, Wi-Fi,
  SimpleUI, 越狱, 微信读书, 主页, weread.koplugin, or simpleui.
---

# Kindle 改装向导

帮助用户在**自己的** Kindle 上安装 KOReader、微信读书插件，以及默认的乔木 SimpleUI 主页。之后用户要求改界面时，改本地副本再写回设备。人机协作：脚本负责下载、校验和写入已挂载的 Kindle 磁盘；需要在屏幕上点按的步骤由用户完成。

工作目录是本技能根目录。所有命令都从这里运行，并加上 `--json`。日志在 stderr，结果在 stdout。

```bash
python scripts/kindle_helper.py <子命令> --json
```

Windows 用 `python`，macOS / Linux 也可以用 `bash scripts/kindle_helper.sh`。

## 必须遵守

- Kindle 还没连上时，只让用户做一件事。Kindle 一旦连上，同一轮里把电脑能完成的安装全部跑完，不要逐阶段汇报，也不要询问装哪一个插件。越狱确认是 `install-jailbreak` 之前唯一要停下来的事；用户明确同意之后，后面的电脑步骤仍在同一轮做完。
- 只有脚本返回的步骤必须在 Kindle 屏幕上完成时才停下来。把那一条 `message` 交给用户，等对方做完并重新连上，再把剩下的命令一次跑完。
- 安装结束后只说最后一条成功命令的 `message`。那就是怎么打开新界面。不要再讲「设置 → 主页 → 以主页启动」，也不要叫用户自己找工具栏扫码。
- 默认安装 `weread`。只有用户明确要觅阅时才用 `miuread` 或 `both`。
- 只有 `scripts/kindle_helper.py` 可以写 Kindle。不要自己复制、解压或拼下载链接。
- 脚本拒绝写入系统盘。不要用 `--path` 指向用户的主目录或系统目录来“试试看”。
- 下载只使用 `scripts/pins.json` 里的版本和 SHA-256。校验失败就停，不要改下别的 Release，也不要跳过校验。`--mirror` 得到的文件走同一条校验。
- 固件以 Kindle 盘内的 `system/version.txt` 为准。读不到时再问用户，不要猜。
- 架构只按下面的分界判断，不要换成别的安装包：
  - 固件 `< 5.16.3`：`legacy_softfloat`，KOReader 用 `kindlepw2`，KUAL 用 `coplate`。
  - 固件 `>= 5.16.3`：`kindlehf`，KOReader 用 `kindlehf`，KUAL 用 `hardfloat`。
- 基准机是 Kindle Paperwhite 第 7 代（KP3，序列号前缀 `G090`），固件 `5.16.2.1.1`。它走 `kindlepw2` 和 `coplate`。
- 国内或 GitHub 不稳定时，在命令后加 `--mirror https://ghfast.top/`。镜像只重试文件下载；失败时把脚本的 `error` 原样告诉用户。
- 运行 `install-jailbreak` 之前必须先停下来，把下面这段原样发给用户，等对方回复「同意」。没有这句同意，不要运行命令，也不要自己加上 `--i-accept-jailbreak-risk`。LanguageBreak 还会清空设备，只有用户明确要求才使用。

```text
越狱会让这台 Kindle 失去亚马逊保修。失败时可能变砖：开不了机，或者卡在更新失败。
恢复方法：先把 documents 文件夹复制到电脑；设备还能开机时，按 https://kindlemodding.org/jailbreaking/unjailbreaking.html 退出越狱并装回官方固件；若卡在 Update Failed，按 https://kindlemodding.org/jailbreaking/recovering-from-a-reset.html 把对应机型的官方固件 .bin 放到 Kindle 根目录后重启；AdBreak 改过的广告文件可以用 restore-assets 还原；电脑完全认不出磁盘时，这个工具修不好。
请回复「同意」才继续。
```
- 乔木 SimpleUI 是默认主页。`install-plugin` 和 `wifi-install-plugin` 会一并装上，不要先问用户要不要。设备上已经有 `simpleui.koplugin` 时不要覆盖。只有用户明确说不要这个主页时才加 `--no-simpleui`。
- 安装会把 KOReader 的 `start_with` 写成 `homescreen_simpleui`，并在还没有 `settings/simpleui` 时跳过布局向导。已经有的个人设置不要覆盖。USB 的 `install-plugin` 和 `install-simpleui` 还会把 `SSH_autostart` 写成 false，包括旧安装留下的 true。无线安装时不要改正在运行的 `settings.reader.lua`；下一次打开由 `wekindle.koplugin` 写入启动项，并把 `SSH_autostart` 写成 false。

## 状态

在对话里记住脚本返回的这些字段：`kindle_path`、`firmware`、`architecture`、`koreader_arch`、`kual_variant`、`jailbreak_method`、`deploy_mode`、`kindle_ip`、`stage`。USB 命令沿用 `--path`，无线命令沿用 `--ip`。

## 阶段 1：先判断 USB 还是 Wi-Fi

新对话不要先要求插线。先探测，再根据结果只说一件事：

```bash
python scripts/kindle_helper.py detect --json
python scripts/kindle_helper.py scan-wifi --json
```

`detect` 找不到磁盘时退出码是 2，这不表示失败。继续看 `scan-wifi`。

- 状态 B：`scan-wifi` 的 `kindle_ip` 有值，或 USB 上 `koreader_present` 为真且用户明确说 Kindle 已连上同一个 Wi-Fi。跳过插拔数据线，直接进入阶段 5 的无线安装。若已经扫到 IP，不要再叫用户插 USB。无线命令连接用户已经手动打开的 2222 端口，可以完成安装。不要改 `settings.reader.lua`，也不要把 `SSH_autostart` 写成 true。
- 状态 B 但没扫到端口：KOReader 已装好。只说一句，让用户临时打开 SSH：打开 KOReader，进入顶部菜单 → 网络 → SSH 服务器，勾选 SSH 服务器和允许无密码登录。用户回复「SSH 已开」后重新运行 `scan-wifi`。
- 无线安装、`wifi-install-simpleui`，或把改过的 SimpleUI 写回设备之后，告诉用户回到同一菜单，取消勾选 SSH 服务器。这些无线命令不改正在运行的 `settings.reader.lua`。下一次完全退出再打开时，`wekindle.koplugin` 会把 `SSH_autostart` 写成 false，旧安装留下的自启动也会关掉。不要另写一份配置把它重新打开。
- 状态 A：USB 上已经找到 Kindle，但 `koreader_present` 为假，并且没有扫到 2222；或者用户明确说还没装 KOReader。告诉用户：因为亚马逊原厂限制，第一次越狱和写入 KOReader 必须用一次 USB。然后请用户打开飞行模式并连上数据线，回复「已连接」。固件 `>= 5.18.1` 时不要先开飞行模式，这种机器要先联网下载锁屏广告，走 AdBreak。
- 两个探测都没有结果，而且用户还没说明设备状态时，只问：已经装好 KOReader，并且和电脑在同一个 Wi-Fi 吗？不要同时讲解两套流程。

用户确认 USB 已连接，或 `scan-wifi` 已经找到 Kindle 之后，按下面的「一次做完」继续。不要先停下来只报告型号。

读不到固件时，只问型号和版本号。用户回答后，后续命令加上 `--firmware 5.16.2.1.1` 这样的值。盘内版本和用户说法冲突时，以盘内为准并告诉用户。用户已经知道 IP 时，用 `scan-wifi --ip <地址> --json`，不要再扫整段网。

## 连上之后一次做完

USB 连上后先运行 `detect` 和 `clean-ota`，再按设备现状往下走。能在电脑上完成的命令不要拆开。

状态 B 没有插上 USB 时，不要运行 `clean-ota`。KOReader 已经在，并且扫到了 2222，就从第 5 步的无线命令开始，然后 `finalize`。

1. 还没越狱：用户已经对上面的确认回复「同意」之后，运行 `install-jailbreak --i-accept-jailbreak-risk`，把 `message` 交给用户，停。这是屏幕上的步骤。还没同意就停在确认，不要先跑命令。
2. 对方回复「已重启」并重新连上后，再按阶段 2 判断要不要 Hotfix。需要的话运行 `install-hotfix` 并停。不需要就继续。
3. 还没有 KUAL：运行 `install-kual`。`already_present` 就继续；否则把 `message` 交给用户，停，等 `;log mrpi` 做完并重新连上。
4. 还没有 KOReader：按架构运行 `install-koreader`。成功后不要把这条 `message` 念给用户，直接装插件。
5. 运行 `install-plugin --choice weread`。状态 B 改为 `wifi-install-plugin --ip <Kindle_IP> --choice weread`。不要问 A 还是 B。这条命令会装上乔木 SimpleUI，并让下一次打开直接进入新主页。
6. 成功后立刻运行 `finalize`。只把 `finalize` 的 `message` 告诉用户。

某一步失败时，只说 `error` 和下一步，不要继续假装已经装完。`simpleui_error` 有内容时，只说明微信读书插件已装好、主页没装上，以及错误原文。

## 阶段 2：越狱与 Hotfix

默认方案来自 `detect` 的 `jailbreak_method`：

- 低于 `5.18.1`：WinterBreak。`5.16.2.1.1` 也用它。
- `>= 5.18.1`：AdBreak。WinterBreak 在这个版本上不可用。
- LanguageBreak 只适用于 `<= 5.16.2.1.1`，而且会清空内容。用户点名时才加 `--method languagebreak --i-understand-this-wipes-the-kindle`。
- 用户要求不登录亚马逊商店、且固件低于 `5.16.4` 时，才改用 `--method winterbreak2`。

用户对确认回复「同意」之后才运行。缺少 `--i-accept-jailbreak-risk` 时脚本会拒绝，不会下载，也不会写入。

```bash
python scripts/kindle_helper.py install-jailbreak --i-accept-jailbreak-risk --json
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

不要让用户手动拖文件夹。按已锁定的架构运行其中一个，然后继续阶段 5，不要把这条命令的 `message` 念出来：

```bash
python scripts/kindle_helper.py install-koreader --arch kindlepw2 --json
python scripts/kindle_helper.py install-koreader --arch kindlehf --json
```

USB 安装会把 `SSH_autostart` 写成 false。SSH 不会随 KOReader 启动，也不会预置无密码登录。已经装过旧版本、设置里仍是 true 的设备，后面的 USB `install-plugin` 或 `install-simpleui` 会再写成 false。这次数据线还插着，插件继续走 USB，不要中途改成无线。

## 阶段 5：微信读书插件和新主页

不要询问装哪一个。默认：

```bash
python scripts/kindle_helper.py install-plugin --choice weread --json
```

状态 B 用无线命令，不要要求插 USB：

```bash
python scripts/kindle_helper.py wifi-install-plugin --ip <Kindle_IP> --choice weread --json
```

用户明确说要觅阅时，才把 `--choice` 换成 `miuread` 或 `both`。同一条命令默认再安装乔木 SimpleUI。设备上已经有 `simpleui.koplugin` 时不会覆盖它。用户明确说不要这个主页时，才加 `--no-simpleui`。

这条命令还会写入启动主页。USB 上直接改 `settings.reader.lua` 的 `start_with`，并把 `SSH_autostart` 写成 false；旧版本留下的 true 也会改掉。无线时 KOReader 还开着，不改这份正在使用的设置，只留下 `wekindle.koplugin`。下一次完全退出再打开时，它写入启动项，并把 `SSH_autostart` 写成 false。还没有 `settings/simpleui` 时会跳过布局选择。已经有的个人设置保持原样。

端口没开时，把返回的 `message` 原样告诉用户，等对方回复「SSH 已开」后再运行同一条命令。用户在 KOReader 里手动打开 SSH 之后，这条命令仍通过 2222 安装。成功后不要停在这条 `message`，继续 `finalize`。`finalize` 会要求用户取消勾选 SSH 服务器。不要把 `SSH_autostart` 写成 true。

脚本会把目录名固定成 `weread.koplugin`，并要求里面有 `main.lua`。无线传输只接受局域网私有地址。

## 打开新界面

`finalize` 的 `message` 是给用户的全部说明：弹出 Kindle、连上 Wi-Fi、从 KUAL 打开 KOReader。第一次会直接进入新主页。Kindle 上会先说明去手机微信读书获取 API Key，用户点「显示二维码」后才出现二维码。不要再补一套菜单路径。

用户确认主页已经生效之前，不要开始改界面。确认之后再没有别的安装步骤。`finalize` 可以和阶段 5 放在同一轮，不要等用户先看完主页才运行它。

## 阶段 6：更新或修改 SimpleUI

初次安装不用进入本阶段。阶段 5 已经默认写入乔木 SimpleUI。

用户要求重装主页、更新到新的 Release，或改主页、底栏、隐藏和其他界面时才做。不要在安装成功的同一条回复里开始改。

乔木 SimpleUI 是 [joeseesun/qiaomu-simpleui](https://github.com/joeseesun/qiaomu-simpleui)，GPL-3.0。它只换主页、底栏和书库展示。阅读和微信读书书架仍由 KOReader 与 `weread.koplugin` 负责。需要 KOReader `2026.03` 或更新，并且启用内置 CoverBrowser。第一次打开时，底栏会有微信读书入口；未登录时 Kindle 自己弹出扫码说明。

重装时用和当前连接相同的通道。脚本只下载 `scripts/pins.json` 里登记的 `simpleui.koplugin.zip`（含预发布；不要用 GitHub Source code ZIP），按清单里的 SHA-256 校验，不一致就停止，镜像下载也一样。目录名固定为 `simpleui.koplugin`。已经装过时会先备份旧目录。`koreader/settings/simpleui`（含隐藏记录）不覆盖；若写入过程碰到它，脚本会撤回插件并写回原设置。

```bash
python scripts/kindle_helper.py install-simpleui --json
python scripts/kindle_helper.py wifi-install-simpleui --ip <Kindle_IP> --json
```

改界面时，改电脑上的 `simpleui.koplugin` 副本，不要改 Kindle 盘里的文件，也不要改 `koreader/settings/simpleui`。KOReader 开着时，让用户临时打开 SSH，写回命令才能连上。USB 写回会把 `SSH_autostart` 写成 false。无线写回不要改正在运行的 `settings.reader.lua`，也不要把 `SSH_autostart` 写成 true；下一次打开时 `wekindle.koplugin` 把它写成 false。一次只改用户刚说的那一处，然后写回：

```bash
python scripts/kindle_helper.py install-simpleui --source <目录> --json
python scripts/kindle_helper.py wifi-install-simpleui --ip <Kindle_IP> --source <目录> --json
```

`<目录>` 可以是 `simpleui.koplugin` 本身，或直接包含它的目录。安装结果里的 `source_path` 是刚解压的副本，`finalize` 清缓存后就没了。把返回的 `message` 发给用户。无线写回的 `message` 会要求完全退出并重新打开 KOReader，然后取消勾选 SSH 服务器。不要把插件源码提交进本仓库。`finalize` 在初次安装时已经运行过，改界面时不必再运行。

## 异常时只说原因和下一步

- 找不到 Kindle：请确认 USB 已连接，必要时在资源管理器中显示隐藏文件，然后回复「已连接」。
- 找到多个盘：请用户看盘符，下一次命令加 `--path`。
- 剩余空间不足：先让用户删除不需要的书。开着 Wi-Fi 时不要为了腾空间而删除文件，避免系统趁机下载更新。
- 下载失败：重试一次；再失败就加上 `--mirror` 后重试同一条子命令。
- 架构冲突：不要加 `--force`，除非用户看过固件版本后明确要求。
- AdBreak 找不到 `.assets`：让用户先联网下载广告，打开 View all ads，再开飞行模式并重连。
- 需要还原 AdBreak 改过的广告文件：`python scripts/kindle_helper.py restore-assets --json`。
- 扫不到 2222：只让用户在 KOReader 里临时打开 SSH 和无密码登录，不要同时改讲 USB 越狱。做完后让用户取消勾选 SSH 服务器。
- 找到多台设备：把 IP 列出来，等用户选定后再传 `--ip`。
- 无线登录失败：不要换端口或猜测密码。仍旧只让用户勾选允许无密码登录。连上并做完之后，让用户取消勾选 SSH 服务器。

## 命令

| 子命令 | 作用 |
| --- | --- |
| `detect` | 找盘、剩余空间、固件、序列号前缀、越狱标记 |
| `clean-ota` | 删除根目录的 `update.bin.tmp.partial` 等升级残留 |
| `install-jailbreak` | 用户确认保修、变砖和恢复方法后，下载并写入越狱包，Hotfix 先放进缓存 |
| `install-hotfix` | 把一个 Hotfix `.bin` 写到根目录 |
| `install-kual` | 写入 MRPI，并把对应 KUAL 的 install `.bin` 放进 `mrpackages` |
| `install-koreader --arch` | 下载清单里对应架构的 KOReader 并合并到根目录 |
| `install-plugin --choice` | 经 USB 安装微信读书插件，默认 `weread`，并装上乔木 SimpleUI、启动主页和扫码引导 |
| `scan-wifi` | 扫描本机所在局域网里开放 2222 的设备，也可用 `--ip` |
| `wifi-install-plugin --ip --choice` | 用 SSH/SFTP 安装微信读书插件，并默认装上乔木 SimpleUI |
| `install-simpleui` | 重装乔木 SimpleUI，或用 `--source` 写回本地改过的副本 |
| `wifi-install-simpleui --ip` | 经 SSH/SFTP 重装乔木 SimpleUI，或用 `--source` 写回本地改过的副本 |
| `finalize` | 同步磁盘并删除下载缓存 |
| `self-check` | 离线检查路由和压缩包安全逻辑 |
