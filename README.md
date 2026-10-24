# python-translation

Translates the string resources of the Kids Activity Tracker Android app (sibling repo
`../kidsactivitytracker`) into Danish, German, Spanish, French and Swedish using the official
DeepL API.

The tool is a three-step pipeline:

```
extract ──► missing_translations.json ──► translate ──► translations.json (+ per-language files)
                                                                        │
                                                                        ▼
                                                              write-back ──► values-<lang>/*.xml
```

## Requirements

- Python 3
- Packages: `beautifulsoup4` (with `lxml` for the XML parser) and `requests`

  ```bash
  pip install beautifulsoup4 lxml requests
  ```

- A DeepL API key (a free key works: <https://www.deepl.com/pro-api>). Free-tier keys end in
  `:fx` — the script picks the correct endpoint automatically.

Set the key before translating:

```bash
export DEEPL_API_KEY="your-key"        # e.g. in ~/.bashrc to make it permanent
```

Do **not** hardcode the key in `translate.py` — this repo is git-tracked.

## Usage

All examples use `RES` as shorthand for the Android resource folder, e.g.
`/home/arbon/code/kidsactivitytracker/app/src/main/res`.

### 1. Extract untranslated strings

```bash
python3 translate.py <RES> [output.json]      # output defaults to missing_translations.json
```

Reads the default `values/*.xml` files and collects every `<string>`, `<plurals>` and
`<string-array>` that carries `tools:ignore="MissingTranslation"`. Strings in the source file
must be marked with that attribute to be picked up. For each entry it records:

| Field    | Meaning                                                              |
| -------- | -------------------------------------------------------------------- |
| `name`   | Android resource name (e.g. `tab_month`)                             |
| `value`  | The English text                                                     |
| `context`| The `<!-- comment -->` on the line above, kept as translation context |
| `type`   | Only for arrays/plurals: `string-array` or `plurals`                  |
| `quantity` | Only for plurals: `one`, `other`, …                                 |
| `index`  | Only for string-arrays: item position (0-based)                      |

Plain strings keep the exact three-key shape; the extra fields only appear on array/plural items.

### 2. Translate with DeepL

```bash
python3 translate.py --translate [input.json] [output.json]
# input defaults to missing_translations.json, output to translations.json
```

Translates every `value` into `da, de, es, fr, sv` (see `TARGET_LANGUAGES` in the script) and
writes:

- `translations.json` — combined output, one entry per (source file, language)
- `translations_da.json` … `translations_sv.json` — one file per language

The output objects have the same shape as the extraction output; only `value` is translated.
A full run is roughly 60–70k characters, well within the free tier's 500k/month.

Details worth knowing:

- Texts are sent in batches of 50 (DeepL's per-request limit).
- Each `<string-array>` is sent as its own request with the Android comment passed as DeepL's
  `context` parameter — short items like `Mon` are ambiguous without it.
- Plural forms are translated individually; this works because the target languages share
  English's `one`/`other` split.
- After translating, the script prints **placeholder warnings** for any string where a format
  placeholder (`%1$s`, `%d`, …) or the escaped apostrophe `\'` did not survive the translation.
  Review those before writing back. (An apostrophe that legitimately disappeared because the
  language rephrased the sentence — e.g. French "de %1$s" — is fine.)

### 3. Write back into the Android app

```bash
python3 translate.py --write-back [translations.json] [--dry-run]
```

For every (source file, language) it maps `values/X.xml` → `values-<lang>/X.xml`, parses the
destination if it already exists and **upserts**: plain strings by `name`, plural items by
`quantity`, array items by index. Missing files are created from scratch.

- Bare ASCII apostrophes are re-escaped (`'` → `\'`) — unescaped ones fail the Android build.
- The `context` comment is written above each element, mirroring the source file. The
  `tools:ignore="MissingTranslation"` marker is never written to locale files (and is removed
  from existing elements) — it belongs on the source strings only.
- `--dry-run` shows what would happen without writing anything.
- Re-running is safe: unchanged entries are skipped ("unchanged, skipped"), changed ones are
  updated in place. You can hand-edit `translations_*.json` to fix a translation and re-run.

## Typical full cycle

```bash
# after changing English source strings:
python3 translate.py /path/to/app/src/main/res missing_translations.json
python3 translate.py --translate
python3 translate.py --write-back
# then rebuild the app (AAPT2 validates the locale files)
```

## Notes

- The locale `values-*/` files are never a source for extraction — only the default `values/`
  folder is read, and only entries marked `tools:ignore="MissingTranslation"`.
- `DEEPL_API_KEY` is only needed for step 2; extraction and write-back work offline.
- DeepL errors are reported with their HTTP status (403 = invalid key, 429 = rate limit,
  456 = quota exhausted).
