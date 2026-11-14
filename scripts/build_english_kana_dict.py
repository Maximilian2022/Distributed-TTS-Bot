#!/usr/bin/env python3
"""
オフライン英語→カタカナ静的辞書 (O(1) ハッシュマップ用) 生成スクリプト

主なデータソース:
1. SudachiDict (Apache License 2.0): コンテナ内/環境内の sudachipy.Dictionary().entries() から
   alkana 未収録の英語固有名詞・外来語・最新IT/SNS用語を抽出
2. 任意の追加辞書ファイル (--extra): JSON / TSV / CSV
3. 任意の JMdict XML (--jmdict): EDRDG JMdict (CC BY-SA) からの外来語カタカナ抽出

出力:
  src/utils/english_kana_dict.json
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

_SCRIPT_FILE = Path(__file__).resolve()
PROJECT_ROOT = _SCRIPT_FILE.parent.parent if _SCRIPT_FILE.exists() else Path.cwd()
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "src" / "utils" / "english_kana_dict.json"

_RE_ENGLISH_ALPHA = re.compile(r"^[a-zA-Z]{2,64}$")
_RE_PURE_KATAKANA = re.compile(r"^[\u30A0-\u30FFー]+$")

_LETTER_READINGS = (
    "エー", "ビー", "シー", "ディー", "イー", "エフ", "ジー", "エイチ",
    "アイ", "ジェイ", "ケイ", "ケー", "エル", "エム", "エヌ", "オー",
    "ピー", "キュー", "アール", "エス", "ティー", "ユー", "ブイ", "ヴィ",
    "ダブリュー", "エックス", "ワイ", "ゼット",
)
_RE_ALPHA_SPELL_ONLY = re.compile(r"^(?:" + "|".join(_LETTER_READINGS) + r")+$")


def _try_import_alkana():
    try:
        import alkana
        return alkana
    except ImportError:
        return None


def extract_from_sudachidict(skip_alkana_known: bool = True) -> Dict[str, str]:
    """SudachiDict (Apache-2.0) から英単語→カタカナ読みを抽出する"""
    try:
        import sudachipy
    except ImportError:
        print("[WARN] sudachipy が見つからないため SudachiDict 抽出をスキップします。", file=sys.stderr)
        return {}

    alkana_mod = _try_import_alkana() if skip_alkana_known else None
    dictionary = sudachipy.Dictionary()
    extracted: Dict[str, str] = {}

    for entry in dictionary.entries():
        surface = entry.surface()
        if not surface or not _RE_ENGLISH_ALPHA.match(surface):
            continue
        # 全大文字のみのエントリは組織コード・純略語が多いため除外（主要略語は COMMON_ACRONYMS / e2k が担当）
        if surface.isupper():
            continue

        reading = entry.reading_form()
        if not reading or not _RE_PURE_KATAKANA.match(reading):
            continue

        lower_key = surface.lower()
        if len(set(lower_key)) == 1 and lower_key[0] == "w":
            # www 等のネットスラング領域は保護
            continue

        if alkana_mod is not None and alkana_mod.get_kana(lower_key):
            continue

        # 単語表記があるのに読みがアルファベット1文字読みの羅列になっているものを除外
        if _RE_ALPHA_SPELL_ONLY.match(reading):
            continue

        extracted.setdefault(lower_key, reading)

    return extracted


def _iter_extra_pairs(path: Path) -> Iterable[Tuple[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("entries", raw) if isinstance(raw, dict) else {}
        if isinstance(entries, dict):
            for k, v in entries.items():
                yield str(k), str(v)
        return

    delimiter = "\t" if suffix in (".tsv", ".dic", ".txt") else ","
    with path.open("r", encoding="utf-8") as f:
        if delimiter == ",":
            reader = csv.reader(f)
            for row in reader:
                if len(row) >= 2:
                    yield row[0], row[1]
        else:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = re.split(r"[\t,]+", s, maxsplit=1)
                if len(parts) == 2:
                    yield parts[0], parts[1]


def extract_from_extra_file(path: Path) -> Dict[str, str]:
    """任意の JSON / TSV / CSV 辞書から有効な英単語→読みペアを抽出する"""
    result: Dict[str, str] = {}
    for raw_k, raw_v in _iter_extra_pairs(path):
        k = raw_k.strip().lower()
        v = raw_v.strip()
        if _RE_ENGLISH_ALPHA.match(k) and v and not (set(k) == {"w"}):
            result[k] = v
    return result


def extract_from_jmdict(jmdict_path: Path) -> Dict[str, str]:
    """
    JMdict XML から英語見出し (keb/gloss) とカタカナ読み (reb) のペアを抽出する。
    (※ JMdict 利用時は CC BY-SA 4.0 の帰属表記に留意)
    """
    result: Dict[str, str] = {}
    context = ET.iterparse(str(jmdict_path), events=("end",))
    for _event, elem in context:
        if elem.tag != "entry":
            continue
        kebs = [k.text.strip() for k in elem.findall("./k_ele/keb") if k.text]
        rebs = [r.text.strip() for r in elem.findall("./r_ele/reb") if r.text]
        kata_rebs = [r for r in rebs if _RE_PURE_KATAKANA.match(r)]
        if kebs and kata_rebs:
            for keb in kebs:
                if _RE_ENGLISH_ALPHA.match(keb) and not keb.isupper():
                    lk = keb.lower()
                    if not (set(lk) == {"w"}):
                        result.setdefault(lk, kata_rebs[0])
        elem.clear()
    return result


def build_dictionary(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    extra_paths: Optional[Iterable[Path]] = None,
    jmdict_path: Optional[Path] = None,
    skip_alkana_known: bool = True,
) -> Dict[str, str]:
    merged: Dict[str, str] = {}

    sudachi_entries = extract_from_sudachidict(skip_alkana_known=skip_alkana_known)
    merged.update(sudachi_entries)

    sources = [f"SudachiDict ({len(sudachi_entries)} entries, Apache-2.0)"]

    if jmdict_path is not None:
        jm_entries = extract_from_jmdict(jmdict_path)
        merged.update(jm_entries)
        sources.append(f"JMdict ({len(jm_entries)} entries)")

    for ep in extra_paths or []:
        ex_entries = extract_from_extra_file(ep)
        merged.update(ex_entries)
        sources.append(f"{ep.name} ({len(ex_entries)} entries)")

    payload = {
        "_meta": {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "count": len(merged),
            "sources": sources,
            "license_notice": "Includes entries extracted from SudachiDict (Apache License 2.0).",
        },
        "entries": dict(sorted(merged.items())),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".json.tmp")
    # 起動時ロード高速化と容量削減のためコンパクト形式で書き出す
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(output_path)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Build O(1) English-to-Katakana JSON dictionary.")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Output JSON path")
    parser.add_argument("--extra", type=Path, action="append", default=[], help="Extra JSON/TSV/CSV dict path")
    parser.add_argument("--jmdict", type=Path, default=None, help="Optional JMdict XML path")
    parser.add_argument(
        "--include-alkana",
        action="store_true",
        help="Include words already present in alkana as well",
    )
    args = parser.parse_args()

    merged = build_dictionary(
        output_path=args.output,
        extra_paths=args.extra,
        jmdict_path=args.jmdict,
        skip_alkana_known=not args.include_alkana,
    )
    print(f"Generated {args.output} with {len(merged)} entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
