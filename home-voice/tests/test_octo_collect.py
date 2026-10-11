"""Check CDN collection without creating or modifying project data."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline


class BundledPathTests(unittest.TestCase):
    def test_relative_paths_and_defaults_follow_project_root_without_external_engine(self):
        root = pipeline.ROOT.parent / 'relocated-home-voice'
        config = {'octo_client': 'octo_source.py', 'character_master': 'data\\Character.yaml'}
        for settings in (config, {}):
            self.assertEqual(pipeline.octo_source_path(settings, root), root / 'octo_source.py')
            self.assertEqual(pipeline.character_master_path(settings, root), root / 'data' / 'Character.yaml')

    def test_absolute_overrides_remain_supported(self):
        module = pipeline.ROOT / 'custom-octo.py'
        master = pipeline.ROOT / 'custom-characters.yaml'
        config = {'octo_client': str(module), 'character_master': str(master)}
        other_root = pipeline.ROOT.parent
        self.assertEqual(pipeline.octo_source_path(config, other_root), module)
        self.assertEqual(pipeline.character_master_path(config, other_root), master)


class CollectTests(unittest.TestCase):
    existing_id = 'sud_vo_system_amao_home_cmmn-01'
    new_id = 'sud_vo_system_amao_home_cmmn-new'
    failed_id = 'sud_vo_system_amao_home_cmmn-failed'

    def job(self):
        job = pipeline.Pipeline.__new__(pipeline.Pipeline)
        job.root = Path(__file__).resolve().parents[1]
        job.config = {'source_mode': 'octo', 'character_master': 'unused'}
        job.voices = [{'voiceAssetId': self.existing_id, 'ja': '人工原文', 'zh': '人工译文', 'reviewed': True}]
        job.save = MagicMock()
        return job

    def collect(self, job, files, failures):
        client = MagicMock()
        client.manifest.return_value = {'version': 205100, 'revision': 7, 'resources': [
            {'name': name + '.acb'} for name in [self.existing_id, self.new_id, self.failed_id, 'sud_vo_amao_battle']]}
        client.download.return_value = ([Path(name + '.acb') for name in files], failures)
        with patch.object(pipeline, 'load_octo_client', return_value=client), patch.object(pipeline, 'write_json'), \
             patch.object(pipeline, 'character_names', return_value={'amao': '麻央'}), \
             patch.object(Path, 'exists', return_value=True), patch.object(Path, 'is_file', return_value=True):
            job.collect()
        return client

    def test_only_new_home_ids_are_downloaded_and_manual_rows_are_preserved(self):
        job = self.job()
        existing = job.voices[0].copy()
        client = self.collect(job, [self.new_id], [])
        self.assertEqual([item['name'] for item in client.download.call_args.args[0]],
                         [self.new_id + '.acb', self.failed_id + '.acb'])
        self.assertEqual(job.voices[0], existing)
        self.assertEqual(job.voices[1]['speaker'], '麻央')
        self.assertEqual(job.voices[1]['ja'], '')
        job.save.assert_called_once()

    def test_partial_failure_registers_success_before_reporting_failure(self):
        job = self.job()
        with self.assertRaisesRegex(RuntimeError, '成功文件已收录'):
            self.collect(job, [self.new_id], [self.failed_id + '.acb'])
        self.assertEqual(len(job.voices), 2)
        self.assertEqual(job.voices[-1]['voiceAssetId'], self.new_id)
        job.save.assert_called_once()

    def test_no_download_does_not_rewrite_reviewed_catalog(self):
        job = self.job()
        self.collect(job, [], [])
        job.save.assert_not_called()
        self.assertTrue(job.voices[0]['reviewed'])

    def test_local_source_still_skips_known_ids_without_calling_network(self):
        job = self.job()
        job.config.update(source_mode='local', source_acb='local-acb')
        with patch.object(pipeline, 'load_octo_client') as network, \
             patch.object(pipeline, 'character_names', return_value={'amao': '麻央'}), \
             patch.object(Path, 'is_dir', return_value=True), patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'iterdir', return_value=iter([Path(self.existing_id + '.acb'), Path(self.new_id + '.acb')])):
            job.collect()
        network.assert_not_called()
        self.assertEqual([row['voiceAssetId'] for row in job.voices], [self.existing_id, self.new_id])
        self.assertEqual(job.voices[0]['zh'], '人工译文')


if __name__ == '__main__':
    unittest.main()
