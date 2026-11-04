# Translation files

The page's own words are English, with Thai written beside them. Any other language is a file here (or in `~/.config/dtree/lang/`,
which wins) named by its code — `ja.json`, `pt-BR.json`, `zh-Hant.json` — of the shape

```json
{"_name": "日本語", "Settings": "設定", "New question": "新しい質問", "Click a node for its details": "…"}
```

Every key is an English string the page shows; a string the file lacks stays English. `dtree lang-template --out ja.json` writes a
file with every string the page shows (and `--code ja` keeps what `ja.json` already has); `dtree lang-check ja` says how many are
translated and which are missing. The questions' own words are not here: they are stored per question (`texts`) and typed on the page.
