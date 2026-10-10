"""Incremental home-voice collection, local Japanese ASR and Chinese translation."""
from __future__ import annotations

import argparse
import copy
import csv
import gc
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VOICE_PATTERN = re.compile(r"^sud_vo_.*_home_.+\.acb$")
CHARACTER_PATTERN = re.compile(r"^sud_vo_system_(?:cidol-)?([a-z0-9]+)(?:-\d+-\d+)?_home_")
EVENT_PATTERN = re.compile(
    r"\[(?:laughs|laughter|chuckles|sighs|gasps|coughs|breathing|music|applause)\]", re.I
)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def event(name: str, **details) -> None:
    print(json.dumps({"event": name, **details}, ensure_ascii=False), flush=True)


def audio_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        if audio.getnframes() <= 0 or audio.getcomptype() != "NONE":
            raise ValueError(f"Empty or unsupported WAV: {path}")
        return {"duration": audio.getnframes() / audio.getframerate(),
                "sample_rate": audio.getframerate(), "channels": audio.getnchannels(),
                "sample_width": audio.getsampwidth()}


def character_names(path: Path) -> dict[str, str]:
    # Character.yaml has top-level '- id' and scalar firstName fields.
    names = {}
    code = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"- id: (\S+)", line)
        if match:
            code = match[1]
        match = re.fullmatch(r"  firstName: (.+)", line)
        if match and code:
            names[code] = match[1].strip('"\'')
    return names


@dataclass
class RecoveredSegment:
    start: float
    end: float
    speaker: str
    text: str


def recover_timed_segments(raw: str) -> list[RecoveredSegment]:
    pattern = r"\[(\d+(?:\.\d+)?)\](?:\[(S\d+)\])?(.*?)\[(\d+(?:\.\d+)?)\]"
    return [RecoveredSegment(float(start), float(end), speaker or "", text)
            for start, speaker, text, end in re.findall(pattern, raw, re.S)
            if text.strip() and float(end) >= float(start)]


def filter_short_repeats(segments: list) -> list:
    kept = []
    for segment in segments:
        text = EVENT_PATTERN.sub("", segment.text).strip()
        previous = EVENT_PATTERN.sub("", kept[-1].text).strip() if kept else ""
        # A whole Japanese sentence repeated in <= 0.2 seconds is a model tail,
        # not a second spoken sentence. Keep short utterances and normal repeats.
        japanese_chars = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", text))
        if text == previous and japanese_chars >= 12 and segment.end - segment.start <= 0.2:
            continue
        kept.append(segment)
    return kept


def impossible_audio_tail(raw: str, segments: list, duration: float) -> bool:
    if not segments:
        return False
    stamps = list(re.finditer(r"\[(\d+(?:\.\d+)?)\]", raw))
    if not stamps:
        return False
    last = stamps[-1]
    start = float(last[1])
    remaining = duration - start
    if start < segments[-1].end or not 0 <= remaining <= 0.75:
        return False
    chars = len(re.findall(r"[\u3040-\u30ff\u4e00-\u9fff]", raw[last.end():]))
    # Retain a completed utterance if an unfinished tail invents more characters
    # than could be spoken in the remaining fraction of this WAV.
    return chars > max(12, math.ceil(remaining * 40))


def clean_text(raw: str, segments: list) -> tuple[str, bool]:
    if segments:
        parts = [EVENT_PATTERN.sub("", seg.text).strip() for seg in filter_short_repeats(segments)]
        return "\n".join(part for part in parts if part), False
    plain = re.sub(r"\[(?:\d+(?:\.\d+)?|S\d+)\]", "", raw)
    return EVENT_PATTERN.sub("", plain).strip(), True


def repeated_transcript(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(r"(.{12,}?)\1+", compact))


def load_hotwords(path: Path) -> list[str]:
    table = read_json(path)
    if not isinstance(table, dict):
        raise ValueError(f"Hotword table must contain grouped Japanese lists: {path}")
    words = []
    for group, entries in table.items():
        if group.startswith("_"):
            continue
        if not isinstance(entries, list):
            raise ValueError(f"Hotword group {group} must be a list")
        for word in entries:
            if not isinstance(word, str) or not word.strip() or "\n" in word or "\r" in word:
                raise ValueError(f"Invalid hotword in {group}: {word!r}")
            word = word.strip()
            if word not in words:
                words.append(word)
    if not words:
        raise ValueError("Hotword table is empty")
    return words


def csv_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def csv_original(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")


def write_bilingual_csv(path: Path, rows: list) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["voiceAssetId", "speaker", "ja", "zh"])
        for row in rows:
            writer.writerow([row["voiceAssetId"], row["speaker"], csv_text(row["ja"]), csv_text(row["zh"])])


def chinese_export_text(text: str) -> str:
    # Normalize observed output residues only; do not change source ASR, raw CSV
    # or configured proper-name spellings such as 手毬.
    return re.sub(r"ふ{2,}", lambda match: "呵" * len(match[0]), text).replace("傭", "佣")


class Pipeline:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.config = read_json(root / "config.json")
        self.catalog_path = root / "data" / "voices.json"
        self.voices = read_json(self.catalog_path, [])
        for row in self.voices:
            row["acb"] = row["acb"].replace("/", "\\")
            row["wav"] = row["wav"].replace("/", "\\")
        self.loaded_rows = {row["voiceAssetId"]: copy.deepcopy(row) for row in self.voices}
        self.results_path = root / "data" / "asr_results.jsonl"

    def save(self, preserve_translations=True):
        latest = {row["voiceAssetId"]: row for row in read_json(self.catalog_path, [])}
        for row in self.voices:
            voice_id = row["voiceAssetId"]
            previous = self.loaded_rows.get(voice_id)
            other = latest.get(voice_id)
            if previous and other and (other.get("ja") != previous.get("ja") or other.get("zh") != previous.get("zh")):
                if not preserve_translations:
                    raise RuntimeError(f"Voice changed during maintenance: {voice_id}; reload before editing")
                # A viewer edit made while ASR was running stays authoritative.
                # Raw ASR is already appended, so its result remains available for review.
                row.clear()
                row.update(other)
        for row in self.voices:
            other = latest.get(row["voiceAssetId"], {})
            if preserve_translations and not row.get("zh") and other.get("zh") and row.get("ja") == other.get("ja"):
                row["zh"] = other["zh"]
                row["translation_model"] = other.get("translation_model", "")
        write_json(self.catalog_path, self.voices)
        self.loaded_rows = {row["voiceAssetId"]: copy.deepcopy(row) for row in self.voices}

    def selected(self, only=None, limit=0):
        rows = [row for row in self.voices if not only or row["voiceAssetId"] in only]
        return rows[:limit] if limit else rows

    def collect(self):
        source = Path(self.config["source_acb"])
        if not source.is_dir():
            raise FileNotFoundError(source)
        names = character_names(Path(self.config["character_master"]))
        indexed = {row["voiceAssetId"]: row for row in self.voices}
        added = 0
        for acb in sorted(source.iterdir()):
            if not acb.is_file() or not VOICE_PATTERN.fullmatch(acb.name):
                continue
            voice_id = acb.stem
            if voice_id in indexed:
                continue
            target = self.root / "audio" / "acb" / acb.name
            if not target.exists():
                shutil.copy2(acb, target)
            match = CHARACTER_PATTERN.match(voice_id)
            code = match[1] if match else ""
            row = {"voiceAssetId": voice_id, "characterCode": code,
                   "speaker": names.get(code, ""), "acb": f"audio\\acb\\{acb.name}",
                   "wav": f"audio\\wav\\{voice_id}.wav", "ja": "", "zh": ""}
            self.voices.append(row)
            indexed[voice_id] = row
            added += 1
        template_path = self.root / "data" / "subtitle_ui.json"
        if not template_path.exists():
            previous = read_json(Path(self.config["subtitle_template"]), {})
            write_json(template_path, {k: v for k, v in previous.items() if k != "subtitles"})
            legacy_ids = {
                "sud_vo_system_cidol-hmsz-3-000_home_cmmn-01",
                "sud_vo_system_cidol-hmsz-3-000_home_cmmn-02",
                "sud_vo_system_cidol-hmsz-3-000_home_cmmn-03",
                "sud_vo_system_cidol-hmsz-3-001_home_cmmn-01",
            }
            write_json(self.root / "data" / "previous_user_subtitles.json",
                       [row for row in previous.get("subtitles", []) if row["voiceAssetId"] in legacy_ids])
        self.save()
        event("collected", new=added, total=len(self.voices))

    def convert(self, only=None, limit=0):
        tool = self.root / "tools" / "vgmstream-win64" / "vgmstream-cli.exe"
        reused = converted = 0
        for row in self.selected(only, limit):
            wav = self.root / row["wav"]
            if not wav.exists():
                previous_wav = Path(self.config["reuse_wav"]) / wav.name
                if previous_wav.exists():
                    audio_info(previous_wav)
                    shutil.copy2(previous_wav, wav)
                    row["wav_source"] = "reused"
                    reused += 1
                else:
                    acb = self.root / row["acb"]
                    subprocess.run([str(tool), "-i", "-o", str(wav), str(acb)], check=True,
                                   stdout=subprocess.DEVNULL)
                    row["wav_source"] = "vgmstream"
                    converted += 1
            row.update(audio_info(wav))
        self.save()
        event("converted", reused=reused, decoded=converted)

    def refresh_asr(self):
        if not self.results_path.exists():
            return
        by_id = {row["voiceAssetId"]: row for row in self.voices}
        for line in self.results_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            result = json.loads(line)
            row = by_id.get(result["voiceAssetId"])
            if row is None:
                continue
            if row.get("ja_origin", "").startswith("manual"):
                continue
            row["asr_error"] = result.get("error", "")
            if repeated_transcript(result.get("text", "")):
                row["asr_error"] = "repeated_transcript"
                if row.get("ja") == result["text"]:
                    row["ja"] = ""
                continue
            if result.get("text") and not result.get("error"):
                # Explicit source edits remain authoritative; automatic reruns only fill gaps.
                if not row.get("ja"):
                    row["ja"] = result["text"]
                row["asr_model"] = result["model"]
                row["asr_review"] = result.get("review", [])

    def asr(self, only=None, limit=0, redo=False):
        self.refresh_asr()
        pending = [row for row in self.selected(only) if redo or not row.get("ja") or row.get("asr_error")]
        pending.sort(key=lambda row: (audio_info(self.root / row["wav"])["duration"], row["voiceAssetId"]))
        if limit:
            pending = pending[:limit]
        if not pending:
            event("asr_complete", pending=0)
            return
        moss = Path(self.config["moss_repo"])
        model_path = moss / "pretrained" / "moss-transcribe-diarize"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HOME"] = str(moss / "pretrained" / "hf-cache")
        sys.path.insert(0, str(moss))
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList
        from moss_transcribe_diarize.app.model_runner import ModelRunner
        from moss_transcribe_diarize.inference_utils import DEFAULT_PROMPT, build_transcription_messages, load_audio_item
        from moss_transcribe_diarize.transcript_parser import parse_transcript

        hotword_path = self.root / self.config.get("hotwords_file", "data\\hotwords.json")
        hotwords = load_hotwords(hotword_path)
        prompt = DEFAULT_PROMPT + "音频是日语，请按原文转写，不要翻译。热词提示：" + ", ".join(hotwords)
        write_json(self.root / "data" / "asr_hotwords.json", hotwords)
        (self.root / "data" / "asr_prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        runner = ModelRunner(model_path, device="cuda:0", dtype="bf16")
        runner._ensure_loaded()
        if runner._device.type != "cuda":
            raise RuntimeError("CUDA is required for this configured batch run")
        model, processor = runner._model, runner._processor
        device, dtype = runner._device, runner._dtype
        tokenizer = processor.tokenizer
        torch.cuda.empty_cache()
        event("asr_start", pending=len(pending), hotwords=len(hotwords), batch_size=self.config["asr_batch_size"])

        class AudioEndCriteria(StoppingCriteria):
            def __init__(self, width, durations):
                self.width = width
                self.durations = durations
                self.finished = [False] * len(durations)
                self.reasons = [""] * len(durations)
                self.lengths = [0] * len(durations)

            def __call__(self, input_ids, scores, **kwargs):
                generated = input_ids[:, self.width:].tolist()
                for index, (duration, tokens) in enumerate(zip(self.durations, generated)):
                    if self.finished[index]:
                        continue
                    if "]" not in tokenizer.decode(tokens[-2:], skip_special_tokens=True) and len(tokens) % 4:
                        continue
                    raw = tokenizer.decode(tokens, skip_special_tokens=True)
                    segments = parse_transcript(raw) or recover_timed_segments(raw)
                    if segments and segments[-1].end >= duration - 0.12:
                        self.finished[index] = True
                        self.reasons[index] = "audio_end"
                        self.lengths[index] = len(tokens)
                    elif impossible_audio_tail(raw, segments, duration):
                        self.finished[index] = True
                        self.reasons[index] = "impossible_unfinished_tail"
                        self.lengths[index] = len(tokens)
                return torch.tensor(self.finished, dtype=torch.bool, device=input_ids.device)

        def infer(rows, tokens, instruction, loop_recovery=False):
            paths = [self.root / row["wav"] for row in rows]
            texts = [processor.apply_chat_template(build_transcription_messages(path, instruction),
                     tokenize=False, add_generation_prompt=True) for path in paths]
            audios = [load_audio_item(str(path), processor.feature_extractor.sampling_rate) for path in paths]
            with torch.inference_mode(), torch.amp.autocast("cuda", dtype=dtype):
                inputs = processor(text=texts, audio=audios, max_length=131072,
                                   audio_kwargs={"device": str(device)}, return_tensors="pt").to(device)
                ids, mask = inputs["input_ids"], inputs["attention_mask"]
                left_ids = torch.full_like(ids, tokenizer.pad_token_id)
                left_mask = torch.zeros_like(mask)
                for index, length in enumerate(mask.sum(dim=1).tolist()):
                    left_ids[index, -length:] = ids[index, :length]
                    left_mask[index, -length:] = 1
                inputs["input_ids"], inputs["attention_mask"] = left_ids, left_mask
                config = copy.deepcopy(model.generation_config)
                config.max_new_tokens, config.do_sample = tokens, False
                if instruction != prompt:
                    # Mild repetition pressure helps a failed greedy decode exit
                    # token loops while keeping the full dictionary prompt.
                    config.repetition_penalty = 1.1
                if loop_recovery:
                    config.repetition_penalty = 1.2
                    config.no_repeat_ngram_size = 8
                ending = AudioEndCriteria(ids.shape[1], [audio_info(path)["duration"] for path in paths])
                outputs = model.generate(**inputs, generation_config=config,
                                         stopping_criteria=StoppingCriteriaList([ending]))
            eos = config.eos_token_id
            eos_ids = set(eos if isinstance(eos, list) else [eos])
            results = []
            for index, (row, output) in enumerate(zip(rows, outputs)):
                generated = output[ids.shape[1]:].tolist()
                if ending.lengths[index]:
                    generated = generated[:ending.lengths[index]]
                end = next((index + 1 for index, value in enumerate(generated) if value in eos_ids), len(generated))
                generated = generated[:end]
                raw = tokenizer.decode(generated, skip_special_tokens=True).strip()
                segments = parse_transcript(raw) or recover_timed_segments(raw)
                text, plain = clean_text(raw, segments)
                truncated = len(generated) >= tokens and not (set(generated[-1:]) & eos_ids)
                review = []
                if text and re.search(r"[\u4e00-\u9fff]", text) and not re.search(r"[\u3040-\u30ff]", text):
                    review.append("no_japanese_kana")
                item = {"voiceAssetId": row["voiceAssetId"], "text": text, "raw": raw,
                        "segments": [asdict(segment) for segment in segments], "model": str(model_path),
                        "generated_tokens": len(generated), "plain_text_output": plain,
                        "repetition_penalty": getattr(config, "repetition_penalty", 1.0),
                        "no_repeat_ngram_size": getattr(config, "no_repeat_ngram_size", 0),
                        "stopped_at_audio_end": ending.finished[index],
                        "stopping_reason": ending.reasons[index],
                        "removed_short_repeats": len(segments) - len(filter_short_repeats(segments)),
                        "review": review, "hotword_table": str(hotword_path.relative_to(self.root)),
                        "hotwords": hotwords,
                        "created_at": datetime.now(timezone.utc).isoformat()}
                if not text:
                    item["error"] = "empty_transcript"
                elif truncated:
                    item["error"] = "generation_truncated"
                elif repeated_transcript(text):
                    item["error"] = "repeated_transcript"
                results.append(item)
            return results

        def safe_infer(rows, tokens, instruction, loop_recovery=False):
            try:
                return infer(rows, tokens, instruction, loop_recovery)
            except torch.cuda.OutOfMemoryError:
                if len(rows) == 1:
                    raise
            # Release exception frames and their live tensors before splitting.
            gc.collect()
            torch.cuda.empty_cache()
            midpoint = len(rows) // 2
            event("asr_split_after_oom", batch_size=len(rows))
            return (safe_infer(rows[:midpoint], tokens, instruction, loop_recovery)
                    + safe_infer(rows[midpoint:], tokens, instruction, loop_recovery))

        completed = failures = 0
        started = time.perf_counter()
        size = self.config["asr_batch_size"]
        with self.results_path.open("a", encoding="utf-8") as output_file:
            for offset in range(0, len(pending), size):
                batch = pending[offset:offset + size]
                results = safe_infer(batch, self.config["asr_max_tokens"], prompt)
                for row, result in zip(batch, results):
                    if result.get("error") or result["review"]:
                        output_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                        output_file.flush()
                        event("asr_retry", voiceAssetId=row["voiceAssetId"], reason=result.get("error") or result["review"])
                        retry_prompt = (DEFAULT_PROMPT + "请只转写实际听到的日语原文，不要翻译，"
                                        "不要重复不存在的语音。热词提示：" + ", ".join(hotwords))
                        result = safe_infer([row], max(512, self.config["asr_max_tokens"] * 2), retry_prompt)[0]
                        if result.get("error") in {"generation_truncated", "repeated_transcript"}:
                            output_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                            output_file.flush()
                            event("asr_retry_loop", voiceAssetId=row["voiceAssetId"], reason=result["error"])
                            loop_prompt = retry_prompt + "请将笑声标记为[laughs]，不要无限重复拟声字符。"
                            result = safe_infer([row], 512, loop_prompt, loop_recovery=True)[0]
                    output_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                    if result.get("error"):
                        row["asr_error"] = result["error"]
                        failures += 1
                    else:
                        if row.get("zh") and row.get("ja") != result["text"]:
                            row["zh"] = ""
                            row.pop("translation_model", None)
                            row.pop("zh_normalization", None)
                            row.pop("zh_origin", None)
                        row["ja"] = result["text"]
                        row["ja_origin"] = "asr"
                        row["asr_error"] = ""
                        row["asr_model"] = result["model"]
                        row["asr_review"] = result["review"]
                    completed += 1
                output_file.flush()
                self.save()
                torch.cuda.empty_cache()
                event("asr_progress", completed=completed, total=len(pending), failures=failures,
                      elapsed_sec=round(time.perf_counter() - started, 1),
                      peak_gpu_mb=round(torch.cuda.max_memory_allocated() / 1024**2),
                      reserved_gpu_mb=round(torch.cuda.memory_reserved() / 1024**2))
        if failures:
            raise RuntimeError(f"{failures} ASR entries failed; successful rows are saved")

    def prepare(self, only=None, limit=0):
        self.refresh_asr()
        groups = {}
        for row in self.selected(only, limit):
            if row.get("ja") and not row.get("zh"):
                groups.setdefault(row["characterCode"] or "unknown", []).append(row)
        batches = []
        size = self.config["translation_batch_size"]
        for code, rows in sorted(groups.items()):
            existing = [int(match[1]) for path in (self.root / "translation" / "input").glob(f"home_{code}_*.csv")
                        if (match := re.fullmatch(rf"home_{re.escape(code)}_(\d+)\.csv", path.name))]
            first_number = max(existing, default=0) + 1
            for index in range(0, len(rows), size):
                batch_number = first_number + index // size
                filename = f"home_{code}_{batch_number:03d}.csv"
                path = self.root / "translation" / "input" / filename
                with path.open("w", encoding="utf-8", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(["id", "name", "text", "trans"])
                    for row in rows[index:index + size]:
                        writer.writerow([row["voiceAssetId"], row["speaker"], csv_text(row["ja"]), ""])
                    writer.writerow(["info", f"home_voice/{code}/{batch_number:03d}.txt", "", ""])
                    writer.writerow(["译者", "", "", ""])
                batches.append(filename)
        write_json(self.root / "translation" / "batches.json", batches)
        self.save()
        event("translation_prepared", pending=sum(len(rows) for rows in groups.values()), batches=len(batches))

    def translate(self):
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Node.js is not available")
        subprocess.run([node, str(self.root / "translate.cjs"), str(self.root)], check=True,
                       cwd=self.config["translation_engine"])
        self.voices = read_json(self.catalog_path, [])

    def export(self):
        self.refresh_asr()
        for row in self.voices:
            normalized = chinese_export_text(row.get("zh", ""))
            if normalized != row.get("zh", ""):
                row["zh"] = normalized
                row["zh_normalization"] = "chinese_export_text"
        by_id = {row["voiceAssetId"]: row for row in self.voices}
        if len(by_id) != len(self.voices):
            raise ValueError("Duplicate voiceAssetId in catalog")
        if len({row["voiceAssetId"] for row in self.voices if row.get("zh")}) != len(self.voices):
            raise ValueError("Untranslated voices remain; run status to inspect them")
        config = read_json(self.root / "data" / "subtitle_ui.json", {})
        config["subtitles"] = [{"voiceAssetId": row["voiceAssetId"], "text": row["zh"]} for row in self.voices]
        write_json(self.root / "exports" / "home_voice_subtitles.json", config)
        write_json(self.root / "exports" / "home_voice_bilingual.json",
                   [{"voiceAssetId": row["voiceAssetId"], "speaker": row["speaker"],
                     "ja": row["ja"], "zh": row["zh"]} for row in self.voices])
        write_bilingual_csv(self.root / "exports" / "home_voice_bilingual.csv", self.voices)
        self.save()
        event("exported", subtitles=len(self.voices), target=str(self.root / "exports"))

    def clear_translations(self, only):
        if not only:
            raise ValueError("Select voice IDs explicitly before retranslating")
        for row in self.selected(only):
            row["zh"] = ""
            for key in ("translation_model", "zh_normalization", "zh_origin"):
                row.pop(key, None)
        self.save(preserve_translations=False)

    def import_corrections(self, path: Path):
        with path.open(encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not {"voiceAssetId", "ja", "zh"}.issubset(reader.fieldnames or []):
                raise ValueError("CSV requires voiceAssetId, ja and zh columns")
            updates = list(reader)
        by_id = {row["voiceAssetId"]: row for row in self.voices}
        seen = set()
        # Validate the whole import before applying any edits.
        for item in updates:
            voice_id = item["voiceAssetId"]
            if voice_id not in by_id or voice_id in seen:
                raise ValueError(f"Unknown or duplicate voice ID: {voice_id}")
            if item.get("ja") is None or item.get("zh") is None or not item["ja"].strip():
                raise ValueError(f"Incomplete correction row: {voice_id}")
            seen.add(voice_id)
        changed = invalidated = 0
        for item in updates:
            row = by_id[item["voiceAssetId"]]
            ja, zh = csv_original(item["ja"]), csv_original(item["zh"])
            ja_changed, zh_changed = ja != row["ja"], zh != row["zh"]
            if not ja_changed and not zh_changed:
                continue
            if ja_changed and not zh_changed:
                zh = ""
                invalidated += 1
            if ja_changed:
                row.update(ja=ja, ja_origin="manual_csv", asr_error="", asr_review=[])
            row["zh"] = zh
            row["zh_origin"] = "manual_csv" if zh else ""
            row.pop("translation_model", None)
            row.pop("zh_normalization", None)
            changed += 1
        self.save(preserve_translations=False)
        event("corrections_imported", changed=changed, invalidated_chinese=invalidated)

    def progress(self):
        self.refresh_asr()
        source = Path(self.config.get("source_acb", ""))
        known = {row["voiceAssetId"] for row in self.voices}
        new_ids = sorted(path.stem for path in source.iterdir()
                         if path.is_file() and VOICE_PATTERN.fullmatch(path.name) and path.stem not in known) if source.is_dir() else []
        return {"total": len(self.voices), "new_ids": new_ids, "source_available": source.is_dir(),
                "missing_acb": [row["voiceAssetId"] for row in self.voices if not (self.root / row["acb"]).is_file()],
                "missing_wav": [row["voiceAssetId"] for row in self.voices if not (self.root / row["wav"]).is_file()],
                "pending_asr": [row["voiceAssetId"] for row in self.voices if not row.get("ja") or row.get("asr_error")],
                "pending_translation": [row["voiceAssetId"] for row in self.voices if row.get("ja") and not row.get("zh")],
                "asr_errors": [row["voiceAssetId"] for row in self.voices if row.get("asr_error")],
                "review": [row["voiceAssetId"] for row in self.voices if row.get("asr_review")]}

    def status(self):
        self.refresh_asr()
        event("status", total=len(self.voices),
              acb=sum((self.root / row["acb"]).is_file() for row in self.voices),
              wav=sum((self.root / row["wav"]).is_file() for row in self.voices),
              japanese=sum(bool(row.get("ja")) for row in self.voices),
              chinese=sum(bool(row.get("zh")) for row in self.voices),
              asr_errors=[row["voiceAssetId"] for row in self.voices if row.get("asr_error")],
              review=[row["voiceAssetId"] for row in self.voices if row.get("asr_review")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["run", "collect", "convert", "asr", "prepare", "translate", "export", "status"])
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--redo-asr", action="store_true", help="Explicitly regenerate selected ASR, preserving old raw records")
    parser.add_argument("--root", type=Path, default=ROOT, help="Project data directory")
    args = parser.parse_args()
    pipeline = Pipeline(args.root.resolve())
    stages = ["collect", "convert", "asr", "prepare", "translate", "export", "status"] if args.stage == "run" else [args.stage]
    for stage in stages:
        function = getattr(pipeline, stage)
        if stage == "asr":
            function(set(args.only) if args.only else None, args.limit, args.redo_asr)
        elif stage in {"convert", "prepare"}:
            function(set(args.only) if args.only else None, args.limit)
        else:
            function()


if __name__ == "__main__":
    main()
