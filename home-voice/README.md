# Gakumas Home Voice Translate

Windows 本地主页语音处理仓库：收录 ACB、复用或转换 WAV、通过 OpenMOSS 转写日语，再复用既有翻译引擎生成简体中文字幕。

本项目已完整合入 `chihya72/gakumas-translation-work` 的 `home-voice/`。原语音仓库的提交历史、人工校对状态和处理资料保留；viewer 与剧情工作台共用工作仓库权限，语音每次“完成校对”独立生成一个 commit。

## 使用

进入本项目目录后运行 `python run.py`，打开编号菜单：

本机目录：`D:\GIT\gakumas-translation-work\home-voice`。执行菜单前先拉取工作仓库最新 `main`，保留线上协作者的日中校对；完成本地更新后，将 `home-voice/` 下文本和新增 ACB 一起提交到工作仓库。现有本地 WAV 已复制到该目录，仍由本目录 `.gitignore` 忽略。

| 编号 | 操作 |
| --- | --- |
| 1 | 扫描并收录新增 ACB，仅按未收录 ID 增量加入 |
| 2 | 准备 WAV，已有文件复用，缺失时由 vgmstream 转换 |
| 3 | OpenMOSS 日语识别，仅处理缺失或失败项 |
| 4 | 全量翻译所有有日文、无中文的条目 |
| 5 | 导出日中 JSON/CSV 和正式中文字幕 |
| 6 | 指定 ID/角色重识别、重翻译，导出/导入校对 CSV |
| 7 | 一键按 1→2→3→4→5 增量更新 |
| 8 | 查看进度、失败和待复核条目 |
| 9 | 检测环境、修改路径和批次、管理本地热词 |
| 0 | 退出 |

也可以运行 `python run.py --step 3`，或用 `python run.py --check` 只检查环境。菜单启动无需在当前 Python 中安装 Torch；第 3 步自动使用 `config.json` 中 OpenMOSS 的独立虚拟环境。

第 3 步自动启动专用识别子进程，结束后自动关闭并释放模型内存和显存，不启动 OpenMOSS 网页服务。每次启动的进程树归属独立 Windows Job Object，Ctrl+C 只停止本次任务及其子进程，已完成批次保留；不会查找或结束其他程序。

仍保留原来的 PowerShell 阶段入口，在 PowerShell 7 中运行：

```powershell
& (Join-Path 'D:\GIT\gakumas-translation-work\home-voice' 'run.ps1')
```

默认依次执行 collect、convert、asr、prepare、translate、export、status。再次运行仅收录尚未存在的 voiceAssetId，并补齐缺失 WAV、日语和中文。已有 ID 的源 ACB 更新不自动覆盖；需要重处理时由用户明确指定并维护相应数据。

也可以只运行一个阶段，例如：

```powershell
& (Join-Path 'D:\GIT\gakumas-translation-work\home-voice' 'run.ps1') -Stage status
& (Join-Path 'D:\GIT\gakumas-translation-work\home-voice' 'run.ps1') -Stage asr -Only @('sud_vo_system_amao_home_cmmn-03')
```

## 数据和依赖

- `tools\vgmstream-win64`：完整本地转换工具及许可证，纳入 Git。
- `audio\acb`：原始主页语音副本，纳入 Git；源文件不移动或删除。
- `audio\wav`：识别输入，保存在本地，Git 忽略。
- `data\hotwords.json`：独立维护的本地日语热词表，分为人名、昵称、歌曲/作品标题、专有词。精简掉客人、学生等通用标签，补齐当前主要角色全名及常用名字。ASR 使用所有分类并去重，不按角色筛选、不注入角色卡或人物背景、不混入中文译法，也不机械替换识别稿。
- `data\name_dictionary.json`：首次收录时复制的旧词典，仅作历史资料；运行时不读取它，也不读取外部 name_dictionary.json。更新本地热词不会自动重识别旧条目；需要时使用第 6 项指定范围。
- `data\asr_hotwords.json`、`asr_prompt.txt`：实际使用的热词与提示。
- `data\asr_results.jsonl`：全新识别的原始输出、时间段和异常重识别记录，按尝试追加保存；早期调试记录也保留，当前采用的文本以 `voices.json` 为准。首次全量处理的最终 1549 条识别全部使用完整的 302 个字典热词。
- `data\voices.json`：按 voiceAssetId 关联音频、说话人、日语、中文和模型来源。可人工校对 ja、zh；已有非空值不会在普通增量运行中覆盖。
- `data\previous_user_subtitles.json`：单独保留旧任务的四条用户已有文本；新项目仍对全部音频重新识别。游戏中的现有字幕不修改。
- `translation\input`、`output`：现有引擎兼容的 CSV，保留完整语音 ID。
- `exports\home_voice_bilingual.json`、`.csv`：日中对照数据。CSV 每条语音占一行，日中字段使用字面量 `\n`，不含字段内部的实际换行；导入时还原换行。JSON 保留实际换行的语义。
- `exports\home_voice_subtitles.json`：当前 HV-10 使用的数组格式，text 为简体中文，保留原有 UI 配置。

外部依赖路径在 `config.json` 中配置。OpenMOSS 复用已有独立 Python、CUDA/BF16 模型和缓存，不复制模型。翻译直接加载 GakumasPreTranslation 的 `.env`、API 实现、模型、角色卡、术语表和只读翻译记忆，不复制密钥，不修改该仓库。按主表角色码读取说话人，主页语音使用独立 home_voice 分类，不注入连续剧情上下文。

ASR 默认每批 8 条，显存不足时自动拆分。翻译每批最多 250 条，同一批只包含一个角色；默认并行处理 3 个独立批次，可通过 `translation_concurrency` 调整。任一请求失败后停止分派新批次，让已经发出的请求保存成功结果。后续生成的 CSV 使用新的批次编号，保留此前输入与输出。

导出时将中文译文中机翻遗留的连续日语笑声 `ふふ` 规范为对应的 `呵呵`，将已发现的繁体字“傭”规范为“佣”，并在总表记录处理标记；日语识别原文和原始机翻 CSV 保留不变。角色名沿用既有配置，包括“手毬／小毬”的写法。

原始 ASR 与机翻均为自动稿，热词只能辅助模型，不保证姓名完全正确。解析失败、截断、整句重复或疑似输出中文的识别会使用更明确的转写提示单条重试，并以 1.1 的重复惩罚避免贪心解码陷入字符循环；重试仍使用全部本地热词，原始记录保留。使用第 6 项或 `-Stage asr -RedoAsr` 可明确重识别选中范围；原文发生变化时清空对应机翻，防止错误复用旧译文。翻译成功逐批保存，再次运行跳过已翻译行；失败请求不在外层原样重复。中文缺失时不会导出不完整的正式字幕文件。

ASR 在生成的完整片段结束时间达到 WAV 实测时长时停止，允许 0.12 秒的末尾时间戳误差；也会停止在剩余音频时长内明显不可能说完的未闭合尾段，只保留此前完整的转写片段，避免声音结束后继续凭热词生成内容。缺说话人标签但保留时间戳的输出也可解析。只清理与前段全文相同、至少 12 个日语字符却落在不超过 0.2 秒内的伪重复尾段，保留正常时长的重复发言。原始输出和完整分段均保留。

若第一次重试仍发生截断或整句循环，再进行一次有限的解码恢复：重复惩罚 1.2、禁止重复 8-token 片段，并要求用 `[laughs]` 标记笑声。正常识别不启用这项限制，所有尝试都保留本地热词和实际解码参数。

## 校对与 viewer

第 6 项可按完整 voiceAssetId、角色码、`@ID列表文件` 或 `*` 选择范围。导出 `exports\home_voice_review.csv`，修改 ja/zh 后再导入。修改日文而不修改中文时，清空对应旧中文；同时填写的新中文会保留。导入先检查所有 ID 和字段，再整体应用。之后执行第 4 步补齐中文、第 5 步导出。

`D:\GIT\gakumas-viewer` 已接入“主页语音”页，上线后自动从 `chihya72/gakumas-translation-work` 的 `main` 分支下 `home-voice/` 读取数据，用 vgmstream WebAssembly 在浏览器中按需解码 ACB。状态为全部、AI识别待复核、待校对、已校对。日中编辑自动暂存草稿，使用 viewer 原有 GitHub 登录后点击“完成校对”，只保存本条文本并标记为已校对，在工作仓库生成一个 commit。原稿区与仓库内 CSV 显示字面量 `\n`，输入框显示实际换行。页面没有目录选择和导入导出按钮；本地 CSV 维护仍使用 run.py 第 6 项。WAV 只在浏览器内存或本地识别目录中存在，不进 GitHub。

viewer 保存的人工日文标记为 `manual_viewer`，CSV 导入标记为 `manual_csv`，普通增量流程保留人工稿。识别期间总表若发生外部校对，保留该修改，原始识别结果仍写入日志供复核。

公开项目：[chihya72/gakumas-translation-work/home-voice](https://github.com/chihya72/gakumas-translation-work/tree/main/home-voice)。在线校对入口：[主页语音](https://chihya72.github.io/gakumas-viewer/home-voice)。本地更新完成后将文本与 ACB 同步到工作仓库 main；WAV、模型和 API 密钥不上传。本项目不部署游戏文件、不启动或重启游戏。
