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

`translate` either calls the DeepL API directly, or routes through the godeepl-server
proxy (sibling repo `../gotranslation`) via `DEEPL_BASE_URL` — see
[Translation proxy](#translation-proxy-godeepl-server).

## Requirements

- Python 3.11 or later
- Packages: `beautifulsoup4` (with `lxml` for the XML parser) and `requests`

  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -r requirements.txt
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

Plain text strings keep the three-key shape. Arrays and plurals include the extra
fields listed above. Any resource containing inline XML also includes `"markup": true`;
its `value` contains serialized XML, including namespace declarations needed by
`<xliff:g>` placeholders. Leading/trailing whitespace and spaces around inline tags
are preserved. Existing reports without `markup` continue to be treated as plain text.
Re-extract older reports to recover markup or whitespace previously removed.

### 2. Translate with DeepL

```bash
python3 translate.py --translate [input.json] [output.json] [--override]
# input defaults to missing_translations.json, output to translations.json
```

Translates every `value` into `da, de, es, fr, sv` (see `TARGET_LANGUAGES` in the script) and
writes:

- `translations.json` — combined output, one entry per (source file, language)
- `translations_da.json` … `translations_sv.json` — one file per language

With `DEEPL_BASE_URL` exported (see [Translation proxy](#translation-proxy-godeepl-server))
the same command routes through the godeepl-server instead — every text travels under its
Android name, translations are cached in the server's database, and the client's
`DEEPL_API_KEY` can be any dummy value.

The output objects have the same shape as the extraction output; only `value` is translated.
A full run is roughly 60–70k characters, well within the free tier's 500k/month.

Details worth knowing:

- Texts are sent in batches of 50 (DeepL's per-request limit).
- Batches containing inline markup use [DeepL XML handling](https://github.com/DeepL/openapi/blob/main/openapi.yaml)
  (`tag_handling: xml`) and `ignore_tags: ["xliff:g"]` to keep placeholder contents
  untranslated. Plain text in these batches is XML-escaped before sending and decoded
  after translation. Plain-text-only batches keep their existing request format.
  XML options create a separate proxy cache variant; an initial markup translation
  may therefore call DeepL even if an older plain-text translation was cached.
- Write-back restores inline elements as XML nodes and escapes apostrophes in text,
  without changing attribute values. Malformed translated XML is rejected before
  writing its destination file. Review translated formatting before applying it.
- DeepL's API has no per-text id field: request and response correlate purely by array
  position. The script carries the Android ids alongside every request and verifies the
  response count per batch — if DeepL ever returned fewer or more translations than texts
  sent, the run aborts with a message naming the affected strings (`hello..pending`) instead
  of silently shifting translations against the wrong ids.
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
- Once an entry has been written into **every** language folder (`da, de, es, fr, sv`), its
  `tools:ignore="MissingTranslation"` marker is removed from the original `values/` file, so the
  next extraction run no longer reports it as missing. Entries translated into only some
  languages keep their marker. Other values in `tools:ignore` (e.g. `Typos`) are preserved, and
  passing a per-language file like `translations_da.json` removes nothing — only the combined
  `translations.json` covers all languages.
- `--dry-run` shows what would happen without writing anything.
- Re-running is safe: unchanged entries are skipped ("unchanged, skipped"), changed ones are
  updated in place. You can hand-edit `translations_*.json` to fix a translation and re-run.

## Translation proxy (godeepl-server)

The [godeepl-server](../gotranslation) is a caching proxy in front of DeepL: it keeps the
real API key server-side and caches every translation in its database under the Android
resource name. Point the client at it with a dummy key:

```bash
export DEEPL_BASE_URL="http://127.0.0.1:8080"
export DEEPL_API_KEY="local"   # any non-empty value; the proxy replaces it with the real key
python3 translate.py --translate
```

In this mode every text is sent under its Android name and translations are matched back
by id — never by position. The client aborts if an id is missing from the response or
returned twice.

```json
request:  POST {DEEPL_BASE_URL}/v2/translate
          {"source_lang": "EN", "target_lang": "DA", "preserve_formatting": true,
           "texts": [{"id": "screen_title_about", "text": "About", "context": "Screen title"}]}

response: {"translations": [{"id": "screen_title_about", "text": "Om",
                             "detected_source_language": "EN"}]}
```

`context` is the `<!-- comment -->` above the string in the source file, omitted when
there is none. Texts are batched 50 per request — DeepL's per-request limit, since the
proxy forwards each same-options group to DeepL in one call. Errors keep DeepL's
`{"message": ...}` shape and status codes, so they read exactly like direct-API errors.

**Caching caveat:** the proxy keys its cache by id, languages and options — deliberately
*not* by source text. If you edit an English string, its id stays the same and the proxy
returns the previously cached translation. To refresh translations after changing source
text, use:

```bash
python3 translate.py --translate missing_translations.json translations.json --override
```

`--override` requires `DEEPL_BASE_URL` and sends the JSON boolean `"override": true`
on every proxy batch, for every target language. The server skips cached values and
replaces saved translations for the requested IDs and effective settings. Without
this flag, cached translations are reused as before. Other language/context variants
are retained. The flag is handled by the Go server and never forwarded to DeepL.
Refreshing uses DeepL again and consumes translation quota. It only affects resources
in the input report; mark changed source entries with `MissingTranslation` before
extracting them again.

## Typical full cycle

```bash
# after changing English source strings:
python3 translate.py /path/to/app/src/main/res missing_translations.json
python3 translate.py --translate          # set DEEPL_BASE_URL to use the godeepl-server proxy
python3 translate.py --write-back
# fully translated strings lose their MissingTranslation marker in values/,
# so the next extraction only reports what is still missing
# then rebuild the app (AAPT2 validates the locale files)
```

## Notes

- The locale `values-*/` files are never a source for extraction — only the default `values/`
  folder is read, and only entries marked `tools:ignore="MissingTranslation"`.
- `DEEPL_API_KEY` is only needed for step 2 (any dummy value when `DEEPL_BASE_URL` is set);
  extraction and write-back work offline.
- DeepL errors are reported with their HTTP status (403 = invalid key, 429 = rate limit,
  456 = quota exhausted).

## Development

Install dependencies using the virtual environment instructions above, then run:

```bash
python -m unittest discover -v
```

Tests cover XML extraction and write-back (including whitespace, formatting tags,
arrays, plurals, and namespaced placeholders), repeated write-back, placeholder
counts, request batching, response ID mapping, and the proxy override flag. API
requests are mocked: tests do not require credentials or spend translation quota.
GitHub Actions runs the suite on Python 3.11 and 3.14. Live DeepL translation and
Android builds are not part of this test suite.
