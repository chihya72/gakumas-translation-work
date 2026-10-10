"""Exercise ID preservation, incremental ingest and source/translation separation."""
import importlib.util
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("pipeline", Path(__file__).resolve().parents[1] / "pipeline.py")
pipeline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pipeline
spec.loader.exec_module(pipeline)


class PipelineTests(unittest.TestCase):
    def test_viewer_correction_during_asr_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            pipeline.write_json(root / "config.json", {})
            row = {"voiceAssetId": "voice", "speaker": "麻央", "ja": "古い", "zh": "旧译文", "acb": "a", "wav": "w"}
            pipeline.write_json(root / "data" / "voices.json", [row])
            asr = pipeline.Pipeline(root)
            manual = dict(row, ja="校正済み", zh="人工校对", ja_origin="manual_viewer")
            pipeline.write_json(root / "data" / "voices.json", [manual])
            asr.voices[0].update(ja="再認識", zh="")
            asr.save()
            self.assertEqual(pipeline.read_json(asr.catalog_path)[0], manual)

    def test_chinese_export_normalizes_retained_japanese_laughter_only(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            pipeline.write_json(root / "config.json", {})
            raw_csv = root / "translation" / "output" / "original.csv"
            raw_csv.parent.mkdir(parents=True)
            raw_csv.write_text("raw translated CSV: 好热\\nふふふ\\n小森傭、手毬", encoding="utf-8")
            original_bytes = raw_csv.read_bytes()
            job = pipeline.Pipeline(root)
            job.voices = [{"voiceAssetId": "voice", "speaker": "広", "ja": "熱い\nふふふ",
                           "zh": "好热\nふふふ\n小森傭、手毬", "acb": "audio\\acb\\voice.acb",
                           "wav": "audio\\wav\\voice.wav"}]
            job.export()
            result = pipeline.read_json(root / "exports" / "home_voice_subtitles.json")
            self.assertEqual(result["subtitles"][0]["text"], "好热\n呵呵呵\n小森佣、手毬")
            catalog = pipeline.read_json(root / "data" / "voices.json")
            self.assertEqual(catalog[0]["ja"], "熱い\nふふふ")
            self.assertEqual(catalog[0]["zh"], "好热\n呵呵呵\n小森佣、手毬")
            self.assertEqual(raw_csv.read_bytes(), original_bytes)

    def test_new_translation_batch_preserves_previous_request_and_result_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            (root / "translation" / "input").mkdir(parents=True)
            (root / "translation" / "output").mkdir()
            pipeline.write_json(root / "config.json", {"translation_batch_size": 250})
            job = pipeline.Pipeline(root)
            job.voices = [{"voiceAssetId": "voice", "characterCode": "amao", "speaker": "麻央",
                           "ja": "最初", "zh": "", "acb": "audio\\acb\\voice.acb",
                           "wav": "audio\\wav\\voice.wav"}]
            job.prepare()
            original = root / "translation" / "input" / "home_amao_001.csv"
            original_bytes = original.read_bytes()
            result = root / "translation" / "output" / original.name
            result.write_text("previous paid response", encoding="utf-8")
            job.voices[0]["ja"] = "次の原文"
            job.prepare()
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(result.read_text(encoding="utf-8"), "previous paid response")
            self.assertEqual(pipeline.read_json(root / "translation" / "batches.json"), ["home_amao_002.csv"])

    def test_invented_hotword_tail_does_not_hide_a_completed_utterance(self):
        raw = "[0.00][S01]朝食はしっかり食べてください。[3.50][3.50][S01]" + "おばあちゃん、" * 4
        segments = pipeline.recover_timed_segments(raw)
        self.assertTrue(pipeline.impossible_audio_tail(raw, segments, 3.68))
        self.assertEqual(pipeline.clean_text(raw, segments)[0], "朝食はしっかり食べてください。")
        short_tail = "[0.00][S01]朝食はしっかり食べてください。[3.50][3.50][S01]はい"
        self.assertFalse(pipeline.impossible_audio_tail(short_tail, segments, 3.68))

    def test_timestamp_recovery_and_impossible_tail_repeat_keep_original_words(self):
        sentence = "お腹すいて午前の授業集中できなかった。"
        raw = f"[0.00][S01]{sentence}[2.98][2.98][S01]{sentence}[3.14]"
        segments = pipeline.recover_timed_segments(raw)
        text, plain = pipeline.clean_text(raw, segments)
        self.assertEqual(text, sentence)
        self.assertFalse(plain)
        segments[-1].end = 6.0
        self.assertEqual(pipeline.clean_text(raw, segments)[0], sentence + "\n" + sentence)

    def test_missing_speaker_tag_retains_timestamps_for_audio_endpoint(self):
        raw = "[0.00]たまにはゆっくり話すのもいいですね[3.26]"
        segments = pipeline.recover_timed_segments(raw)
        self.assertEqual(segments[0].end, 3.26)
        self.assertEqual(segments[0].speaker, "")
        self.assertEqual(pipeline.clean_text(raw, segments)[0], "たまにはゆっくり話すのもいいですね")

    def test_repeated_model_output_is_detected_without_deleting_real_short_repetitions(self):
        sentence = "昨日、相談してくれた子の様子、見てこようかな"
        self.assertTrue(pipeline.repeated_transcript(sentence + sentence))
        self.assertFalse(pipeline.repeated_transcript("ふふ、ふふ"))
        self.assertFalse(pipeline.repeated_transcript(sentence))

    def test_local_hotwords_ignore_metadata_and_deduplicate_all_groups(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            path = Path(directory) / "hotwords.json"
            pipeline.write_json(path, {"_说明": "不进提示", "characters": ["藤田ことね", "ことね"],
                                       "aliases": ["ことね", "広"], "terms": ["初星学園"]})
            self.assertEqual(pipeline.load_hotwords(path), ["藤田ことね", "ことね", "広", "初星学園"])

    def test_csv_keeps_literal_newlines_and_import_invalidates_stale_chinese(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            pipeline.write_json(root / "config.json", {})
            row = {"voiceAssetId": "voice", "speaker": "麻央", "ja": "最初\n原文", "zh": "旧\n译文",
                   "acb": "voice.acb", "wav": "voice.wav"}
            job = pipeline.Pipeline(root)
            job.voices = [row]
            job.save()
            path = root / "review.csv"
            pipeline.write_bilingual_csv(path, [row])
            self.assertEqual(len(path.read_text(encoding="utf-8-sig").splitlines()), 2)
            with path.open(encoding="utf-8-sig", newline="") as source:
                exported = list(csv.DictReader(source))[0]
            self.assertEqual(exported["ja"], "最初\\n原文")
            self.assertEqual(exported["zh"], "旧\\n译文")
            corrected = dict(row, ja="校正\n原文")
            pipeline.write_bilingual_csv(path, [corrected])
            job.import_corrections(path)
            loaded = pipeline.Pipeline(root)
            self.assertEqual(loaded.voices[0]["ja"], "校正\n原文")
            self.assertEqual(loaded.voices[0]["zh"], "")
            job.results_path.write_text(json.dumps({"voiceAssetId": "voice", "text": "過去", "error": "old"}) + "\n")
            loaded.refresh_asr()
            self.assertEqual(loaded.voices[0]["ja"], "校正\n原文")
            self.assertFalse(loaded.voices[0]["asr_error"])
            pipeline.write_bilingual_csv(path, [dict(corrected, zh="新\n译文")])
            loaded.import_corrections(path)
            self.assertEqual(pipeline.Pipeline(root).voices[0]["zh"], "新\n译文")
            loaded.clear_translations({"voice"})
            self.assertEqual(pipeline.Pipeline(root).voices[0]["zh"], "")

    def test_bad_correction_import_is_atomic(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            pipeline.write_json(root / "config.json", {})
            row = {"voiceAssetId": "voice", "speaker": "麻央", "ja": "原文", "zh": "译文", "acb": "a", "wav": "w"}
            job = pipeline.Pipeline(root)
            job.voices = [row]
            job.save()
            before = job.catalog_path.read_bytes()
            path = root / "bad.csv"
            pipeline.write_bilingual_csv(path, [dict(row, ja="校正"), dict(row, voiceAssetId="unknown")])
            with self.assertRaises(ValueError):
                job.import_corrections(path)
            self.assertEqual(job.catalog_path.read_bytes(), before)

    def test_incremental_ingest_keeps_original_audio_and_manual_text(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            for name in ("source", "audio\\acb", "data", "exports"):
                (root / name).mkdir(parents=True)
            voice_id = "sud_vo_system_cidol-amao-3-000_home_cmmn-01"
            source = root / "source" / (voice_id + ".acb")
            source.write_bytes(b"old audio")
            (root / "source" / "sud_vo_system_amao_idol_x.acb").write_bytes(b"other")
            master = root / "Character.yaml"
            master.write_text("- id: amao\n  firstName: 麻央\n", encoding="utf-8")
            pipeline.write_json(root / "config.json", {"source_acb": str(root / "source"),
                "character_master": str(master), "subtitle_template": str(root / "missing.json")})
            job = pipeline.Pipeline(root)
            job.collect()
            self.assertEqual(len(job.voices), 1)
            self.assertEqual(job.voices[0]["speaker"], "麻央")
            job.voices[0].update(ja="手動\n原文", zh="人工\n译文")
            job.save()
            source.write_bytes(b"new audio")
            job.collect()
            self.assertEqual((root / job.voices[0]["acb"]).read_bytes(), b"old audio")
            self.assertEqual(job.voices[0]["ja"], "手動\n原文")
            job.export()
            output = pipeline.read_json(root / "exports" / "home_voice_subtitles.json")
            self.assertEqual(output["subtitles"], [{"voiceAssetId": voice_id, "text": "人工\n译文"}])

    def test_failed_export_does_not_replace_previous_complete_export(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            root = Path(directory)
            pipeline.write_json(root / "config.json", {})
            previous = root / "exports" / "home_voice_subtitles.json"
            pipeline.write_json(previous, {"subtitles": [{"text": "已完成"}]})
            job = pipeline.Pipeline(root)
            job.voices = [{"voiceAssetId": "new", "ja": "新しい", "zh": ""}]
            with self.assertRaises(ValueError):
                job.export()
            self.assertEqual(pipeline.read_json(previous)["subtitles"][0]["text"], "已完成")


if __name__ == "__main__":
    unittest.main()
