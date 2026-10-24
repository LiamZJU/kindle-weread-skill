# kindle-weread-skill

把这句话发给你的 Agent：

```text
安装这个 Skill 的 v0.1：https://github.com/LiamZJU/kindle-weread-skill/tree/v0.1 ，帮我的 Kindle 装上 KOReader、微信读书插件和乔木 Simple UI
```

用 Claude Code、Codex 或 Cursor 和你一起，把**自己的** Kindle 装上开源阅读器 [KOReader](https://github.com/koreader/koreader)、社区微信读书插件，以及默认的乔木 SimpleUI 主页。界面不满意，就让助手改这份插件再写回 Kindle。

脚本负责查找 Kindle 磁盘、下载公开的社区安装包、校验并写入。Kindle 连上之后，电脑这边的安装会一次做完。需要在 Kindle 屏幕上点按的步骤才会单独停下来。它不会在没有你确认的情况下假装越狱已经成功。

第一次打开 KOReader 会直接进入新主页。Kindle 上会提示先在手机微信读书获取 API Key，然后显示微信二维码。

基准测试机是 Kindle Paperwhite 第 7 代（KP3，序列号前缀 `G090`），固件 `5.16.2.1.1`。这台机器使用软浮点包 `kindlepw2` 和经典 coplate KUAL。

## 开始前

越狱会让这台 Kindle 失去亚马逊保修。失败时可能变砖：开不了机，或者卡在更新失败。请只对自己拥有的 Kindle 操作，并先把 `documents` 文件夹复制到电脑。设备还能开机时，可以按 [KindleModding 的退出越狱说明](https://kindlemodding.org/jailbreaking/unjailbreaking.html) 装回官方固件。若卡在 Update Failed，按 [恢复说明](https://kindlemodding.org/jailbreaking/recovering-from-a-reset.html) 把对应机型的官方固件 `.bin` 放到 Kindle 根目录后重启。电脑完全认不出磁盘时，这个工具修不好。

`install-jailbreak` 在加上 `--i-accept-jailbreak-risk` 之前不会下载或写入。只有在读过上面的保修、变砖和恢复说明并决定继续时才加这个参数。微信读书插件是非官方客户端，登录的是你自己的账号；请遵守微信读书的使用条款，账号风险由使用者承担。

开始时打开飞行模式，避免 Kindle 在改装过程中静默下载固件。固件 `5.18.1` 及以上要走 AdBreak，需要先联网下载锁屏广告，这种机器先不要开飞行模式。

需要：

- Python 3.9 或更高版本，不需要安装第三方包
- 一根能传数据的 USB 线
- 能访问 GitHub。不稳定时给命令加 `--mirror https://ghfast.top/`

## 在 Cursor 里使用

用 Cursor 打开本仓库。根目录的 `.cursorrules` 会让助手进入改装向导，并按 `SKILL.md` 推进。直接说：

> 帮我把 Kindle 装上 KOReader 和微信读书。

也可以把本仓库复制到个人技能目录：

```text
Windows:  %USERPROFILE%\.cursor\skills\kindle-weread-skill\
macOS/Linux: ~/.cursor/skills/kindle-weread-skill/
```

## 在 Claude Code 里使用

把本仓库放到 Claude Code 的技能目录，使 `SKILL.md` 位于技能根目录：

```bash
mkdir -p ~/.claude/skills
git clone --branch v0.1 https://github.com/LiamZJU/kindle-weread-skill.git ~/.claude/skills/kindle-weread-skill
```

然后说「帮我给 Kindle 安装 KOReader 和微信读书」。助手会按 `SKILL.md` 调用 `scripts/kindle_helper.py`。

## 也可以自己运行脚本

在仓库根目录执行。macOS 和 Linux 可以把 `python` 换成 `bash scripts/kindle_helper.sh`。

```bash
python scripts/kindle_helper.py self-check
python scripts/kindle_helper.py detect --json
python scripts/kindle_helper.py clean-ota --json
python scripts/kindle_helper.py install-jailbreak --i-accept-jailbreak-risk --json
python scripts/kindle_helper.py install-hotfix --json
python scripts/kindle_helper.py install-kual --json
python scripts/kindle_helper.py install-koreader --arch kindlepw2 --json
python scripts/kindle_helper.py install-plugin --choice weread --json
python scripts/kindle_helper.py finalize --json
```

`install-plugin` 默认安装 `weread`，并一起装上乔木 SimpleUI。设备上已经有 SimpleUI 时不会覆盖。只有明确不要这个主页时才加 `--no-simpleui`。装完后的下一次打开会直接进入新主页；还没登录时，Kindle 会引导扫描微信二维码。

`--json` 把结果写到 stdout，进度写到 stderr。没有接上 Kindle 时，除 `self-check` 以外的命令会停下来，不会写入电脑上的其他磁盘。

### 固件和安装包

| 固件 | 架构 | KOReader | KUAL |
| --- | --- | --- | --- |
| `< 5.16.3`，例如 `5.16.2.1.1` | `legacy_softfloat` | `koreader-kindlepw2-*.zip` | coplate booklet，放进 `mrpackages` 后在搜索栏输入 `;log mrpi` |
| `>= 5.16.3` | `kindlehf` | `koreader-kindlehf-*.zip` | 硬浮点 booklet，同样用 `;log mrpi` |

越狱方案默认是 [WinterBreak](https://github.com/KindleModding/WinterBreak)。固件 `>= 5.18.1` 时改为 [AdBreak](https://github.com/KindleModding/AdBreak)。LanguageBreak 只会清空设备，脚本要求额外确认，不作为默认方案。

默认是 `weread`：[finlater/weread.koplugin](https://github.com/finlater/weread.koplugin)。它和乔木 SimpleUI 的微信读书按钮接在一起。扫码前要在手机微信读书打开「我 → 设置 → 微信读书 Skill」并获取 API Key；第一次打开时，Kindle 会把这件事说在屏幕上，再显示二维码。

用户明确要求时才改用 `miuread`：[miumiupy98-art/miuread-koreader](https://github.com/miumiupy98-art/miuread-koreader)。它的功能更多，书架管理是另一次授权。

安装位置固定为 `koreader/plugins/weread.koplugin/`。扫码引导是本仓库自带的 `koreader/plugins/wekindle.koplugin/`。

默认主页是 [乔木 SimpleUI](https://github.com/joeseesun/qiaomu-simpleui)。它基于 SimpleUI 3.0.0（GPL-3.0）：隐藏顶部下拉箭头、在底栏放微信读书入口、长按隐藏书籍并可恢复。上面的 `install-plugin` 会一起装上它。脚本只下载 `scripts/pins.json` 里的 `simpleui.koplugin.zip`，不使用 GitHub Source code ZIP，也不把这份 GPL 插件放进本仓库。需要 KOReader 2026.03 或更新，以及已启用的 CoverBrowser。

已经有 SimpleUI 时，安装微信读书插件不会覆盖它。要重装或改界面时，改电脑上的副本，再用单独的命令写回去。重装会先备份旧目录，并留下 `koreader/settings/simpleui` 里的布局和隐藏记录。

```bash
python scripts/kindle_helper.py install-simpleui --source simpleui.koplugin --json
python scripts/kindle_helper.py wifi-install-simpleui --ip 192.168.1.20 --source simpleui.koplugin --json
```

### 常用参数

```bash
python scripts/kindle_helper.py detect --path E:\ --json
python scripts/kindle_helper.py install-koreader --arch kindlehf --mirror https://ghfast.top/ --json
python scripts/kindle_helper.py install-plugin --choice both --json
```

`--path` 在 macOS 上类似 `/Volumes/Kindle`，在 Linux 上类似 `/media/<用户>/Kindle`。镜像前缀也可以放到环境变量 `KINDLE_GITHUB_MIRROR`。

## 兼容范围

| 设备 | 序列号前缀 | 固件 | 路线 | 验证 |
| --- | --- | --- | --- | --- |
| Kindle Paperwhite 第 7 代（KP3） | `G090` | `5.16.2.1.1` | WinterBreak，KOReader `kindlepw2`，coplate KUAL | 已验证 |
| 其他型号、其他序列号前缀或其他固件 | — | — | 脚本仍按固件选择 WinterBreak、AdBreak、`kindlepw2` 或 `kindlehf` | 未验证 |

已验证的只有这一台：Paperwhite 第 7 代 KP3，序列号前缀 `G090`，固件 `5.16.2.1.1`。表里其余行都是未验证。

## 怎样算安装完成

下面三件事都发生，才算安装完成：

1. 第一次打开 KOReader 时进入乔木 SimpleUI。
2. 按 Kindle 屏幕上的说明，在手机微信读书获取 API Key，扫描二维码后登录成功。
3. 从主页打开一本书，正文能显示。

## 失败时如何恢复

动手前把 `documents` 复制到电脑。

- 越狱文件已经写上、但还没在 Kindle 屏幕上点过 WinterBreak：先不要点商店图标。后续处理按 [WinterBreak 说明](https://kindlemodding.org/jailbreaking/WinterBreak/) 里的 LocalStorage 和 Factory Reset。AdBreak 改过的广告文件，用 `python scripts/kindle_helper.py restore-assets --json` 还原 `system/.assets`。
- 原厂界面还能开机，只是不想留 KOReader：删除 Kindle 上的 `koreader` 目录和 `extensions/koreader`。KUAL 可以留着。书仍在 `documents`。
- 想回到官方系统：按 [退出越狱](https://kindlemodding.org/jailbreaking/unjailbreaking.html)，先恢复 OTA，再恢复出厂，然后安装官方固件 `.bin`。这一步会卸掉越狱。
- 卡在 Update Failed：按 [恢复说明](https://kindlemodding.org/jailbreaking/recovering-from-a-reset.html)，把对应机型的官方固件 `.bin` 放到 USB 根目录，弹出后重启，等它装完。
- 开不了机：按住电源键大约 40 秒，强制重启。电脑还能看到 Kindle 磁盘时，用上面的官方固件 `.bin`。电脑完全认不出磁盘时，这个工具修不好。

## 官方系统更新

亚马逊推送的系统更新一旦装上，越狱会失效，KOReader 也可能打不开。

改装过程中，这个技能用三件事拦住更新：

1. 开始时打开飞行模式。固件 `5.18.1` 及以上要先联网下载锁屏广告，那种机器先不要开飞行模式，广告下完再断开网络。
2. 写入越狱文件之前运行 `clean-ota`。它删除根目录里的 `update.bin.tmp.partial`、`update.bin.tmp`、`update.bin` 和 `update_kindle*.bin`，避免一次已经下到一半的更新装上。
3. WinterBreak 跑完之后，官方说明里后续更新会被拦住。WinterBreak2 成功时，「更新您的 Kindle」发灰，这是拦住更新之后的正常状态。越狱画面要求安装 Hotfix 时，脚本再写入 `Update_hotfix_universal.bin`。

改装完成后不要点「更新您的 Kindle」，也不要把官方固件 `.bin` 放进根目录，除非你打算按上面的步骤退出越狱。

## 安全边界

写入前，脚本要求目标同时有 `system` 和 `documents`，并且能看到 `version.txt`、`dictionaries` 或其他 Kindle 标记。Windows 会跳过系统盘；macOS 只接受 `/Volumes` 下的非系统卷；Linux 只接受 `/media`、`/mnt` 或 `/run/media`。压缩包里的绝对路径、`..` 和符号链接会被拒绝。

每个会写到 Kindle 上的安装包都列在 `scripts/pins.json`：固定版本、下载地址和 SHA-256。脚本在写入之前计算文件哈希，和清单不一致就删除下载并停止。`--mirror` 换来的文件走同一条校验。

`clean-ota` 只删除 Kindle 根目录中的升级残留，例如 `update.bin.tmp.partial`。它不会删除已经放好的 Hotfix。

AdBreak 会先把原来的 `system/.assets` 备份到本机缓存。需要还原时运行：

```bash
python scripts/kindle_helper.py restore-assets --json
```

## 许可

本仓库的脚本和文档以 MIT 许可证发布。KOReader、越狱包和插件各自使用上游许可证；本工具只在运行时从它们的公开 Release 下载。乔木 SimpleUI 是 GPL-3.0，同样只在安装时下载，不包含在本仓库里。
