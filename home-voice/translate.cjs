'use strict';

// Use the configured engine in place: no copied keys, prompts, cards or API code.
const fs = require('fs');
const path = require('path');
const root = path.resolve(process.argv[2] || __dirname);
const settings = JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8'));
const engine = settings.translation_engine;
const engineRequire = require('module').createRequire(path.join(engine, 'package.json'));
engineRequire('dotenv').config({ path: path.join(engine, '.env') });
process.env.TM_DIR = settings.translation_memory;
process.env.DEAR_SUMMARY_WRITE = '0';
engineRequire('ts-node').register({
  project: path.join(engine, 'tsconfig.json'),
  transpileOnly: true,
  compilerOptions: { module: 'CommonJS', moduleResolution: 'node' },
});
const Papa = engineRequire('papaparse');
const { getLLMConfig, setupLog } = require(path.join(engine, 'src', 'setup-env.ts'));
const { translateCsvString } = require(path.join(engine, 'src', 'translate.ts'));
const { extractInfoFromCsvText } = require(path.join(engine, 'src', 'csv.ts'));

function saveJson(file, value) {
  fs.writeFileSync(file + '.tmp', JSON.stringify(value, null, 2) + '\n', 'utf8');
  fs.renameSync(file + '.tmp', file);
}

function persistProgress(catalogFile, current) {
  const update = new Map(current.map(row => [row.voiceAssetId, row]));
  // Re-read before merging so edits made during a paid request are retained.
  const latest = JSON.parse(fs.readFileSync(catalogFile, 'utf8'));
  for (const row of latest) {
    const translated = update.get(row.voiceAssetId);
    if (translated && !row.zh && row.ja === translated.ja) {
      row.zh = translated.zh;
      row.translation_model = translated.translation_model;
      row.zh_origin = 'machine';
      delete row.zh_normalization;
    }
  }
  saveJson(catalogFile, latest);
  return latest;
}

async function main() {
  setupLog();
  const llmConfig = getLLMConfig();
  const catalogFile = path.join(root, 'data', 'voices.json');
  let catalog = JSON.parse(fs.readFileSync(catalogFile, 'utf8'));
  if (process.argv.includes('--check')) {
    const { characterCardBlock, classifyStory, storyPolicy } = require(path.join(engine, 'src', 'tm.ts'));
    const speakers = new Set(catalog.map(row => row.speaker).filter(Boolean));
    const policy = storyPolicy(classifyStory('home_voice/amao/001.txt').type);
    console.log(JSON.stringify({ event: 'engine_check', model: llmConfig.model,
      max_tokens: llmConfig.max_tokens, speakers: speakers.size,
      character_cards_loaded: !!characterCardBlock(speakers), story_context: policy.context }));
    return;
  }
  const batches = JSON.parse(fs.readFileSync(path.join(root, 'translation', 'batches.json'), 'utf8'));
  let completed = 0;
  async function translateBatch(filename) {
    const batchCatalog = JSON.parse(fs.readFileSync(catalogFile, 'utf8'));
    const input = fs.readFileSync(path.join(root, 'translation', 'input', filename), 'utf8');
    const info = extractInfoFromCsvText(input);
    const byId = new Map(batchCatalog.map(row => [row.voiceAssetId, row]));
    const pending = info.data.filter(row => byId.has(row.id) && !byId.get(row.id).zh);
    if (!pending.length) return;
    for (const row of pending) {
      if (row.text !== byId.get(row.id).ja.replaceAll('\n', '\\n')) {
        throw new Error(`Source changed after prepare: ${row.id}; run prepare again`);
      }
    }
    const requestCsv = Papa.unparse(pending.concat([
      { id: 'info', name: info.jsonUrl, text: '', trans: '' },
      { id: '译者', name: '', text: '', trans: '' },
    ]));
    // One request contains independent home cues from one speaker. The engine's
    // unclassified home_voice path uses cards/glossary and no story references.
    console.log(JSON.stringify({ event: 'translation_start', file: filename, pending: pending.length }));
    const translatedCsv = await translateCsvString(requestCsv, llmConfig, settings.translation_batch_size);
    fs.writeFileSync(path.join(root, 'translation', 'output', filename), translatedCsv, 'utf8');
    const result = extractInfoFromCsvText(translatedCsv);
    const expected = new Map(pending.map(row => [row.id, row]));
    const seen = new Set();
    for (const row of result.data) {
      const source = expected.get(row.id);
      if (!source || seen.has(row.id) || row.text !== source.text || !row.trans.trim()) {
        throw new Error(`Invalid translated row: ${row.id}; output kept for review`);
      }
      seen.add(row.id);
      const target = byId.get(row.id);
      target.zh = row.trans.replaceAll('<br>', '\n').replaceAll('\\n', '\n');
      target.translation_model = llmConfig.model;
    }
    if (seen.size !== pending.length) throw new Error(`Incomplete batch: ${filename}`);
    const latest = persistProgress(catalogFile, batchCatalog.filter(row => seen.has(row.voiceAssetId)));
    completed += seen.size;
    console.log(JSON.stringify({ event: 'translation_progress', file: filename,
      completed_this_run: completed, chinese_total: latest.filter(row => row.zh).length }));
  }
  const concurrency = Math.max(1, Math.min(batches.length || 1, settings.translation_concurrency || 1));
  let next = 0;
  let failure;
  async function worker() {
    while (!failure && next < batches.length) {
      const filename = batches[next++];
      try {
        await translateBatch(filename);
      } catch (error) {
        failure ||= new Error(`${filename}: ${error.message}`);
      }
    }
  }
  await Promise.all(Array.from({ length: concurrency }, worker));
  if (failure) throw failure;
  catalog = JSON.parse(fs.readFileSync(catalogFile, 'utf8'));
  console.log(JSON.stringify({ event: 'translation_complete', chinese_total: catalog.filter(row => row.zh).length }));
}

main().catch(error => {
  // Axios errors can hold authorization headers. Only print the human message.
  console.error(error.message);
  process.exitCode = 1;
});
