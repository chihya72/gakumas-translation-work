"""Numbered Windows menu for incremental home voice maintenance."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline import ROOT, Pipeline, load_hotwords, read_json, write_bilingual_csv, write_json
from process_runner import run_child

MENU = """
1. 扫描并收录新增 ACB
2. 准备 WAV（优先复用，缺失时转换）
3. OpenMOSS 日语识别（只处理缺失或失败项）
4. 翻译全部未翻译文本
5. 导出中文字幕和日中对照
6. 校对与指定语音重跑
7. 一键增量更新（1 → 2 → 3 → 4 → 5）
8. 查看进度、失败和待复核条目
9. 设置路径、批次和本地热词表
0. 退出
"""
GROUP_NAMES = {"characters": "人名", "aliases": "昵称", "songs_and_titles": "歌曲和作品标题", "terms": "专有词"}


def resolve_selection(text: str, voices: list) -> set[str]:
    known = {row["voiceAssetId"] for row in voices}
    if text.strip() in {"*", "all"}:
        return known
    selected = set()
    codes = {row["characterCode"] for row in voices}
    for token in text.replace(",", " ").split():
        if token.endswith((".acb", ".wav")):
            token = token.rsplit(".", 1)[0]
        if token in known:
            selected.add(token)
        elif token in codes:
            selected.update(row["voiceAssetId"] for row in voices if row["characterCode"] == token)
        else:
            raise ValueError(f"未找到语音 ID 或角色码：{token}")
    return selected


class MenuApp:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()

    def job(self):
        return Pipeline(self.root)

    def stage(self, name, only=None, redo=False):
        configuration = self.job().config
        python = (Path(configuration["moss_repo"]) / ".venv" / "Scripts" / "python.exe"
                  if name == "asr" else Path(sys.executable))
        if not python.is_file():
            raise FileNotFoundError(f"Python 不存在：{python}")
        arguments = [str(python), "-u", str(ROOT / "pipeline.py"), name, "--root", str(self.root)]
        if only:
            arguments.extend(["--only", *sorted(only)])
        if redo:
            arguments.append("--redo-asr")
        run_child(arguments, self.root, asr=name == "asr")

    def step(self, number, only=None):
        stages = {1: ["collect"], 2: ["convert"], 3: ["asr"],
                  4: ["prepare", "translate"], 5: ["export"]}
        if number in stages:
            for stage in stages[number]:
                self.stage(stage, only)
        elif number == 6:
            self.maintenance()
        elif number == 7:
            for step in range(1, 6):
                print(f"\n执行第 {step} 步", flush=True)
                self.step(step)
        elif number == 8:
            self.show_progress(details=True)
        elif number == 9:
            self.settings()

    def show_progress(self, details=False):
        progress = self.job().progress()
        print(f"\n总数 {progress['total']} | 新增未收录 {len(progress['new_ids'])} | "
              f"待转换 {len(progress['missing_wav'])} | 待识别 {len(progress['pending_asr'])} | "
              f"待翻译 {len(progress['pending_translation'])} | "
              f"识别失败 {len(progress['asr_errors'])} | 待复核 {len(progress['review'])}")
        if not progress["source_available"]:
            print("ACB 源目录不可用，可在第 9 项修改路径。")
        if details:
            labels = {"new_ids": "新增 ID", "missing_acb": "缺失 ACB", "missing_wav": "缺失 WAV",
                      "pending_asr": "待识别", "pending_translation": "待翻译",
                      "asr_errors": "识别失败", "review": "待复核"}
            for key, label in labels.items():
                if progress[key]:
                    print(f"\n{label}：")
                    for voice_id in progress[key]:
                        print(f"  {voice_id}")

    def select_scope(self):
        job = self.job()
        print("角色码：" + ", ".join(sorted({row["characterCode"] for row in job.voices})))
        text = input("输入完整 ID／角色码（空格分隔）、@ID列表文件或 * 表示全部；回车取消：").strip()
        if text.startswith("@"):
            path = Path(text[1:].strip().strip('"'))
            if not path.is_absolute():
                path = self.root / path
            text = " ".join(line for line in path.read_text(encoding="utf-8-sig").splitlines()
                            if line.strip() and not line.lstrip().startswith("#"))
        only = resolve_selection(text, job.voices)
        if only:
            print(f"已选择 {len(only)} 条语音。")
        return only

    def maintenance(self):
        print("\n1. 指定语音重新识别\n2. 指定语音重新翻译\n3. 导出校对 CSV\n4. 导入校对 CSV\n0. 返回")
        choice = input("选择：").strip()
        if choice in {"1", "2"}:
            only = self.select_scope()
            if not only:
                return
            if choice == "1":
                self.stage("convert", only)
                self.stage("asr", only, redo=True)
            else:
                self.job().clear_translations(only)
                self.step(4, only)
        elif choice == "3":
            job = self.job()
            job.refresh_asr()
            path = self.root / "exports" / "home_voice_review.csv"
            write_bilingual_csv(path, job.voices)
            print(f"校对 CSV 已保存：{path}\n日中字段的换行用字面量 \\n 表示。")
        elif choice == "4":
            text = input("校对 CSV 路径（回车使用 exports\\home_voice_review.csv）：").strip().strip('"')
            path = Path(text) if text else Path("exports\\home_voice_review.csv")
            if not path.is_absolute():
                path = self.root / path
            self.job().import_corrections(path)
            print("导入完成；原文改变且译文未改的条目已清空旧中文，可执行第 4 步补齐，再执行第 5 步导出。")
        elif choice != "0":
            raise ValueError("无效的维护选项")

    def hotword_path(self):
        path = (self.root / self.job().config.get("hotwords_file", "data\\hotwords.json")).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("热词表必须保存在本项目内")
        return path

    def show_hotwords(self):
        path = self.hotword_path()
        words = load_hotwords(path)
        print(f"\n本地热词表：{path}\n去重后 {len(words)} 项，下次识别会使用所有分类。")
        for group, entries in read_json(path).items():
            if not group.startswith("_"):
                print(f"{GROUP_NAMES.get(group, group)}（{len(entries)}）：" + "、".join(entries))

    def edit_hotwords(self):
        path = self.hotword_path()
        table = read_json(path)
        groups = [group for group in table if not group.startswith("_")]
        print("1. 添加热词\n2. 删除热词\n0. 返回")
        action = input("选择：").strip()
        if action == "0":
            return
        if action not in {"1", "2"}:
            raise ValueError("无效的热词操作")
        if action == "1":
            for index, group in enumerate(groups, 1):
                print(f"{index}. {GROUP_NAMES.get(group, group)}")
            index = int(input("添加到哪个分类：")) - 1
            if not 0 <= index < len(groups):
                raise ValueError("无效的分类")
        words = [word.strip() for word in input("输入日语热词，多个用 | 分隔；回车取消：").split("|") if word.strip()]
        if not words:
            return
        if action == "1":
            existing = load_hotwords(path)
            table[groups[index]].extend(word for word in dict.fromkeys(words) if word not in existing)
        else:
            for group in groups:
                table[group] = [word for word in table[group] if word not in words]
            if not any(table[group] for group in groups):
                raise ValueError("热词表不能全部清空")
        write_json(path, table)
        print("本地热词表已保存；不会自动重新识别旧语音。")

    def check_environment(self):
        config = self.job().config
        moss = Path(config["moss_repo"])
        engine = Path(config["translation_engine"])
        paths = {"ACB 源目录": Path(config["source_acb"]), "WAV 复用目录": Path(config["reuse_wav"]),
                 "OpenMOSS Python": moss / ".venv" / "Scripts" / "python.exe",
                 "OpenMOSS 模型": moss / "pretrained" / "moss-transcribe-diarize",
                 "vgmstream": self.root / "tools" / "vgmstream-win64" / "vgmstream-cli.exe",
                 "翻译 API 配置": engine / ".env", "翻译运行依赖": engine / "node_modules",
                 "角色主表": Path(config["character_master"]), "本地热词表": self.hotword_path()}
        for label, path in paths.items():
            print(f"{'可用' if path.exists() else '缺失'} | {label}：{path}")
        print(f"Node.js：{shutil.which('node') or '未找到'}")
        print(f"本地日语热词：{len(load_hotwords(self.hotword_path()))} 项")
        print(f"ASR 每批 {config['asr_batch_size']} 条；翻译每批最多 {config['translation_batch_size']} 条，"
              f"并发 {config.get('translation_concurrency', 1)} 批。")

    def edit_configuration(self):
        path = self.root / "config.json"
        config = read_json(path)
        keys = ["source_acb", "reuse_wav", "moss_repo", "translation_engine", "translation_memory",
                "character_master", "subtitle_template", "asr_batch_size", "asr_max_tokens",
                "translation_batch_size", "translation_concurrency"]
        for index, key in enumerate(keys, 1):
            print(f"{index}. {key} = {config.get(key, '')}")
        text = input("选择要修改的编号；回车取消：").strip()
        if not text:
            return
        index = int(text) - 1
        if not 0 <= index < len(keys):
            raise ValueError("无效的配置编号")
        key = keys[index]
        value = input("新值；回车保留：").strip().strip('"')
        if not value:
            return
        if key in {"asr_batch_size", "asr_max_tokens", "translation_batch_size", "translation_concurrency"}:
            value = int(value)
            if value <= 0:
                raise ValueError("批次、并发和 token 上限必须为正整数")
        else:
            if not Path(value).is_absolute():
                raise ValueError("请填写 Windows 绝对路径")
            value = str(Path(value))
        config[key] = value
        write_json(path, config)
        print("配置已保存。")

    def settings(self):
        print("\n1. 检测环境和目录\n2. 修改路径和批次\n3. 查看本地热词\n4. 添加／删除本地热词\n0. 返回")
        choice = input("选择：").strip()
        actions = {"1": self.check_environment, "2": self.edit_configuration,
                   "3": self.show_hotwords, "4": self.edit_hotwords}
        if choice in actions:
            actions[choice]()
        elif choice != "0":
            raise ValueError("无效的设置选项")

    def interactive(self):
        while True:
            print("\n==== Gakumas Home Voice Translate ====")
            try:
                self.show_progress()
                print(MENU)
                text = input("请输入步骤编号：").strip()
                if text == "0":
                    return
                if text not in {str(index) for index in range(1, 10)}:
                    print("请输入 0～9。")
                    continue
                self.step(int(text))
            except KeyboardInterrupt:
                print("\n已中断本次操作，完成的批次已保存，返回菜单。")
            except EOFError:
                return
            except Exception as error:
                print(f"\n操作失败：{error}\n已完成的数据保留，可修正问题后继续。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, choices=range(10), help="Run one menu step directly")
    parser.add_argument("--check", action="store_true", help="Check local dependencies without ASR or API requests")
    parser.add_argument("--root", type=Path, default=ROOT, help="Project data directory")
    arguments = parser.parse_args()
    app = MenuApp(arguments.root)
    if arguments.check:
        app.check_environment()
    elif arguments.step is None:
        app.interactive()
    elif arguments.step:
        app.step(arguments.step)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n操作已中断，已完成批次保留。")
        sys.exit(130)
    except Exception as error:
        print(f"操作失败：{error}", file=sys.stderr)
        sys.exit(1)
