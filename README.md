# kindle-weread-skill

用 Cursor 或 Claude Code 和你一起，把**自己的** Kindle 装上开源阅读器 [KOReader](https://github.com/koreader/koreader)、社区微信读书插件，以及默认的乔木 SimpleUI 主页。界面不满意，就让助手改这份插件再写回 Kindle。

脚本负责查找 Kindle 磁盘、下载公开的社区安装包、校验并写入。需要在 Kindle 屏幕上点按的步骤会一次只提示一个。它不会在没有你确认的情况下假装越狱已经成功。

基准测试机是 Kindle Paperwhite 第 7 代（KP3，序列号前缀 `G090`），固件 `5.16.2.1.1`。这台机器使用软浮点包 `kindlepw2` 和经典 coplate KUAL。

## 开始前

越狱可能变砖，也会影响保修。请只对自己拥有的 Kindle 操作，并先备份需要留下的文件。微信读书插件是非官方客户端，登录的是你自己的账号；请遵守微信读书的使用条款，账号风险由使用者承担。

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
git clone <本仓库地址> ~/.claude/skills/kindle-weread-skill
```

然后说「帮我给 Kindle 安装 KOReader 和微信读书」。助手会按 `SKILL.md` 调用 `scripts/kindle_helper.py`。

## 也可以自己运行脚本

在仓库根目录执行。macOS 和 Linux 可以把 `python` 换成 `bash scripts/kindle_helper.sh`。

```bash
python scripts/kindle_helper.py self-check
python scripts/kindle_helper.py detect --json
python scripts/kindle_helper.py clean-ota --json
python scripts/kindle_helper.py install-jailbreak --json
python scripts/kindle_helper.py install-hotfix --json
python scripts/kindle_helper.py install-kual --json
python scripts/kindle_helper.py install-koreader --arch kindlepw2 --json
python scripts/kindle_helper.py install-plugin --choice weread --json
python scripts/kindle_helper.py finalize --json
```

`install-plugin` 和 `wifi-install-plugin` 会在微信读书插件之后默认装上乔木 SimpleUI。设备上已经有 SimpleUI 时不会覆盖。只有明确不要这个主页时才加 `--no-simpleui`。

`--json` 把结果写到 stdout，进度写到 stderr。没有接上 Kindle 时，除 `self-check` 以外的命令会停下来，不会写入电脑上的其他磁盘。

### 固件和安装包

| 固件 | 架构 | KOReader | KUAL |
| --- | --- | --- | --- |
| `< 5.16.3`，例如 `5.16.2.1.1` | `legacy_softfloat` | `koreader-kindlepw2-*.zip` | coplate booklet，放进 `mrpackages` 后在搜索栏输入 `;log mrpi` |
| `>= 5.16.3` | `kindlehf` | `koreader-kindlehf-*.zip` | 硬浮点 booklet，同样用 `;log mrpi` |

越狱方案默认是 [WinterBreak](https://github.com/KindleModding/WinterBreak)。固件 `>= 5.18.1` 时改为 [AdBreak](https://github.com/KindleModding/AdBreak)。LanguageBreak 只会清空设备，脚本要求额外确认，不作为默认方案。

插件二选一，也可以两个都装：

- `weread`：[finlater/weread.koplugin](https://github.com/finlater/weread.koplugin)，轻量，扫码前要在微信读书 App 里打开「我 → 设置 → 微信读书 Skill」并获取 API Key。
- `miuread`：[miumiupy98-art/miuread-koreader](https://github.com/miumiupy98-art/miuread-koreader)，功能更多，支持排版美化。书架管理是另一次授权。

安装位置固定为 `koreader/plugins/weread.koplugin/` 或 `koreader/plugins/miuread.koplugin/`。

默认主页是 [乔木 SimpleUI](https://github.com/joeseesun/qiaomu-simpleui)。它基于 SimpleUI 3.0.0（GPL-3.0）：隐藏顶部下拉箭头、在底栏放微信读书入口、长按隐藏书籍并可恢复。上面的 `install-plugin` 会一起装上它。脚本只下载 Release 里的 `simpleui.koplugin.zip`，不使用 GitHub Source code ZIP，也不把这份 GPL 插件放进本仓库。需要 KOReader 2026.03 或更新，以及已启用的 CoverBrowser。

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

## 安全边界

写入前，脚本要求目标同时有 `system` 和 `documents`，并且能看到 `version.txt`、`dictionaries` 或其他 Kindle 标记。Windows 会跳过系统盘；macOS 只接受 `/Volumes` 下的非系统卷；Linux 只接受 `/media`、`/mnt` 或 `/run/media`。压缩包里的绝对路径、`..` 和符号链接会被拒绝。

`clean-ota` 只删除 Kindle 根目录中的升级残留，例如 `update.bin.tmp.partial`。它不会删除已经放好的 Hotfix。

AdBreak 会先把原来的 `system/.assets` 备份到本机缓存。需要还原时运行：

```bash
python scripts/kindle_helper.py restore-assets --json
```

## 许可

本仓库的脚本和文档以 MIT 许可证发布。KOReader、越狱包和插件各自使用上游许可证；本工具只在运行时从它们的公开 Release 下载。乔木 SimpleUI 是 GPL-3.0，同样只在安装时下载，不包含在本仓库里。
