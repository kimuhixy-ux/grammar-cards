#!/usr/bin/env python3
"""Phase 2: pipeline/cache/*.json を統合し、pipeline/output/grammar_items.json を生成する。

- has_grammar_content: true のページ/項目のみ採用する
- grammar_category（完全一致文字列）単位で束ね、同一カテゴリが複数ページ・複数書籍に
  またがる場合は explanation_ja・example_sentences・sources を統合する
- ページ番号はキャッシュファイル名（pipeline/cache/{book}_{page:04d}.json）由来のものを正とする。
  抽出結果JSON内部の "page" フィールドは、見開きスキャンページ等でモデルが印刷ページ番号を
  読み取って返すことがあり、ファイル名の連番と一致しない場合があるため使用しない。
"""
import json
import re
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "pipeline" / "cache"
OUTPUT_DIR = ROOT / "pipeline" / "output"
ID_REGISTRY_PATH = OUTPUT_DIR / "category_id_registry.json"

CACHE_FILE_RE = re.compile(r"^(forest|chigasaki|ex_grammar)_(\d{4})\.json$")


def load_entries() -> list[tuple[str, int, dict]]:
    """(book, page, item) のリストを返す。pageはキャッシュファイル名由来。"""
    entries = []
    for path in sorted(CACHE_DIR.glob("*.json")):
        m = CACHE_FILE_RE.match(path.name)
        if not m:
            continue  # .batch_*.json などの状態ファイルはスキップ
        book, page_str = m.group(1), m.group(2)
        page = int(page_str)
        data = json.loads(path.read_text())
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("has_grammar_content"):
                entries.append((book, page, item))
    return entries


def merge_entries(entries: list[tuple[str, int, dict]]) -> "OrderedDict[str, dict]":
    grouped: "OrderedDict[str, dict]" = OrderedDict()
    for book, page, item in entries:
        category = (item.get("grammar_category") or "").strip()
        if not category:
            continue
        g = grouped.setdefault(category, {
            "grammar_category": category,
            "explanations": [],
            "example_sentences": [],
            "sources": [],
        })

        explanation = (item.get("explanation_ja") or "").strip()
        if explanation and explanation not in g["explanations"]:
            g["explanations"].append(explanation)

        seen_pairs = {(ex["en"], ex["ja"]) for ex in g["example_sentences"]}
        for ex in item.get("example_sentences") or []:
            pair = ((ex.get("en") or "").strip(), (ex.get("ja") or "").strip())
            if pair[0] and pair not in seen_pairs:
                g["example_sentences"].append({"en": pair[0], "ja": pair[1]})
                seen_pairs.add(pair)

        g["sources"].append({
            "book": book,
            "page": page,
            "section_title": (item.get("section_title") or "").strip(),
        })
    return grouped


def load_id_registry() -> "OrderedDict[str, str]":
    """grammar_category文字列 -> item-XXXX ID の永続マッピング。
    Phase 3のカード生成キャッシュ（pipeline/cache/cards/item-XXXX.json）はこのIDに紐づくため、
    書籍を追加するたびにID採番順（キャッシュファイル名のアルファベット順）が変わっても、
    既存カテゴリのIDは変えてはならない（変えるとキャッシュが別カテゴリに誤って結びつく）。
    """
    if ID_REGISTRY_PATH.exists():
        return OrderedDict(json.loads(ID_REGISTRY_PATH.read_text()))
    return OrderedDict()


def save_id_registry(registry: "OrderedDict[str, str]") -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ID_REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2))


def build_output(grouped: "OrderedDict[str, dict]", registry: "OrderedDict[str, str]") -> list[dict]:
    next_idx = max((int(v.split("-")[1]) for v in registry.values()), default=0) + 1
    for category in grouped:
        if category not in registry:
            registry[category] = f"item-{next_idx:04d}"
            next_idx += 1

    result = []
    for category, g in grouped.items():
        result.append({
            "id": registry[category],
            "grammar_category": category,
            "explanation_ja": "\n".join(g["explanations"]),
            "example_sentences": g["example_sentences"],
            "sources": g["sources"],
        })
    result.sort(key=lambda item: int(item["id"].split("-")[1]))
    return result


def main():
    entries = load_entries()
    grouped = merge_entries(entries)
    registry = load_id_registry()
    result = build_output(grouped, registry)
    save_id_registry(registry)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "grammar_items.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    multi_source = [item for item in result if len(item["sources"]) > 1]
    print(f"採用したページ内項目: {len(entries)}件")
    print(f"統合後の文法項目数: {len(result)}件")
    print(f"複数ページ/書籍にまたがる項目: {len(multi_source)}件")
    print(f"出力: {out_path}")

    cat_list_path = OUTPUT_DIR / "grammar_categories.txt"
    cat_list_path.write_text("\n".join(item["grammar_category"] for item in result))
    print(f"カテゴリ一覧（目視チェック用）: {cat_list_path}")


if __name__ == "__main__":
    main()
