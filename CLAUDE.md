# CLAUDE.md — 英文法フラッシュカード学習アプリ（仮称: GrammarCards）

## プロジェクトの目的

2冊の英文法参考書（スキャンPDF）から文法項目・解説・例文を抽出し、
毎日Leitner式間隔反復（SRS）でフラッシュカードを出題するiPhone向けPWAを構築する。

- 学習者: プログラミング初心者（Python/JS実務経験あり）、法律学専門家
- 実行環境: Mac mini M4 / Claude Code CLI
- 既存資産との一貫性: jazz-ireal（GitHub Pages静的PWA）、Wisdom単語帳PWA（Leitner実装済み）、
  theme_bridge（Batches API・ハッシュキャッシュ・差分実行のパターン）を踏襲する

## 素材となるPDFの重要な性質（必ず先に把握すること）

| 書籍 | ページ数 | サイズ | テキスト層 |
|---|---|---|---|
| 総合英語Forest | 642p | A5 | あり（ただし文字化け・信頼不可。`pdftotext`は使用不可） |
| 茅ヶ崎方式国際英語教本文法 | 158p | 大判 | なし（純粋なスキャン画像） |

→ **どちらもCanon複合機でスキャンされた画像PDFであり、通常のテキスト抽出は使えない。**
Vision対応モデル（Claude Haiku/Sonnet）による画像ベースのOCR・構造化抽出が必須。
実装前に必ず10ページ程度のサンプルで抽出精度を検証してから全ページ処理に進むこと。

## ディレクトリ構成

```
grammar_cards/
├── source_pdfs/                   # 元PDF（.gitignore対象、著作権物のためリポジトリに含めない）
│   ├── forest.pdf
│   └── chigasaki.pdf
├── pipeline/                      # Phase1-3: 抽出・カード生成（ローカル実行のPythonスクリプト）
│   ├── 01_extract_pages.py        # PDF→画像化→Vision APIで構造化JSON抽出
│   ├── 02_merge_items.py          # ページ単位JSON→文法項目マスタへ統合
│   ├── 03_generate_cards.py       # 文法項目→問題(カード)生成
│   ├── cache/                     # ページ単位キャッシュ（再実行時スキップ、hashベース）
│   └── output/
│       ├── grammar_items.json     # 統合された文法項目マスタ
│       └── cards.json             # 最終カードバンク（アプリに同梱）
├── docs/                          # GitHub Pages公開ディレクトリ（PWA本体）
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── manifest.json
│   ├── sw.js                      # Service Worker（オフライン対応）
│   ├── icons/
│   └── data/
│       └── cards.json             # pipeline/output/cards.json をコピー
├── CLAUDE.md
├── 進捗.md
├── はじめかた.md
└── .gitignore
```

## Phase 1: ページ抽出（pipeline/01_extract_pages.py）

1. `pdftoppm`で各ページをPNG化する（解像度は150〜200dpi程度。文字が判読できる最低限に抑え、APIコストを節約する）
2. Anthropic **Message Batches API**（`claude-haiku-4-5`、画像入力）で各ページを以下のJSON形式に構造化抽出する:

```json
{
  "page": 60,
  "book": "forest",
  "section_title": "第◯章 ◯◯",
  "grammar_category": "仮定法",
  "explanation_ja": "本文の説明文（要約でよい。逐語転載は避ける）",
  "example_sentences": [
    {"en": "If I were you, ...", "ja": "もし私があなたなら..."}
  ],
  "has_grammar_content": true
}
```

3. 目次・扉・索引・広告ページなど文法内容を含まないページは `has_grammar_content: false` としてスキップ対象にする
4. ページごとに `pipeline/cache/{book}_{page:04d}.json` として保存し、**既にキャッシュが存在するページは再実行時にスキップ**する（theme_bridgeのハッシュキャッシュと同じ方針。PDFファイルが同一であればページ内容も不変なので、キャッシュキーはページ番号のみで十分）
5. 縦書き/横書き混在、圏点、囲み枠、表組みなどレイアウトが複雑なため、プロンプトには「レイアウトの崩れは無視して本文と例文の区別だけ正確に行う」旨を明記する

## Phase 2: 統合（pipeline/02_merge_items.py）

- 全ページのキャッシュJSONを読み込み、`has_grammar_content: true` のもののみ採用
- `grammar_category` 単位で束ね、同一項目が複数ページ・複数書籍にまたがる場合は統合する
- 出力: `pipeline/output/grammar_items.json`
  - 各項目に `sources: [{book, page}, ...]` を保持し、どのページ由来か追跡できるようにする

## Phase 3: カード生成（pipeline/03_generate_cards.py）

- `grammar_items.json` の各項目から `claude-sonnet-4-6` で複数タイプの問題を生成する（Batches APIでコスト削減）
- 出題タイプ（最低これだけは実装する）:
  - `fill_blank`（空所補充）
  - `multiple_choice`（4択）
  - `reorder`（整序英作文）
  - `translate_ja_to_en`（和文英訳）
- カード形式:

```json
{
  "id": "forest-p060-fb-01",
  "book": "forest",
  "page": 60,
  "grammar_category": "仮定法",
  "type": "fill_blank",
  "question": "If I ___ you, I would apologize.",
  "answer": "were",
  "choices": null,
  "explanation": "仮定法過去では be動詞は主語に関わらずwereを用いる"
}
```

- 生成数の目安: 文法項目数 × 3〜5問程度
- 出力: `pipeline/output/cards.json` → そのまま `docs/data/cards.json` にコピーして使う

## Phase 4: iPhone向けPWA（docs/）

- フレームワークなしの素のHTML/CSS/JS（jazz-ireal・stock-flagsと同じ方針。ビルドステップ不要）
- **Leitner式間隔反復**（5箱: 1日/3日/7日/14日/30日）— Wisdom単語帳PWAの実装をそのまま流用してよい
- 学習状態（各カードの箱・次回出題日・正誤履歴）は `localStorage` または `IndexedDB` に保存し、サーバー不要・完全オフラインで動作させる
- `manifest.json` + `sw.js` でホーム画面への追加とオフラインキャッシュに対応する
- 「今日の問題」画面: SRSスケジュールに基づき本日出題対象のカードを抽出し、正誤に応じて箱を昇降
- 任意機能: 文法カテゴリ別の復習モード、書籍別フィルタ（Forest / 茅ヶ崎方式で見比べる）、正答率ダッシュボード

## デプロイ

- GitHub Pages（`docs/`フォルダ）— jazz-irealと同じ運用方法
- 元PDF・`pipeline/`の中間生成物（画像・キャッシュ）はリポジトリに含めない（`.gitignore`）。公開してよいのは `docs/data/cards.json` のみ

## 著作権に関する注意（必ず守ること）

- 元テキストの逐語的な大量転載は避け、解説は要約、例文は学習目的で最小限の引用にとどめる
- 生成したカード・解説文は「参考書の要点を独自に構造化・言い換えたもの」とし、原文ページ画像そのものは一切リポジトリ・公開ディレクトリに含めない
- 個人の学習目的での利用に限定する

## 実装順序（Claude Codeへの依頼順）

1. サンプル抽出のPoC（各書籍10ページ程度）→ 抽出精度を人間がレビュー
2. 抽出プロンプトの調整
3. 全ページのバッチ抽出（Batches APIのジョブ投入・完了待ち・キャッシュ保存）
4. `grammar_items.json` への統合
5. カード生成（Batches API）
6. PWA実装（Leitnerロジックは単語帳PWAから移植）
7. GitHub Pagesへのデプロイ・iPhoneでのホーム画面追加確認

## コスト・時間の見積もり指針

- 総ページ数は約800ページ。Haikuでの画像入力 + Batches APIの割引を前提に見積もる
- 一度全処理が終われば、以後は教材追加時の差分実行のみで済む設計にする
- 大量画像を一度に投げるとコストが跳ねるため、必ず少数ページのテストで生成JSONの質・トークン消費を確認してから全ページに進むこと
