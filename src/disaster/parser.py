"""
防災情報（地震・津波・気象警報）のパースおよび日本語読み上げ・表示テキスト生成ユーティリティ
"""

from typing import Dict, Any, Optional, Tuple, List

# P2P地震情報の震度スケール変換
SCALE_MAP = {
    10: "震度1",
    20: "震度2",
    30: "震度3",
    40: "震度4",
    45: "震度5弱",
    46: "震度5弱以上と推定",
    50: "震度5強",
    55: "震度6弱",
    60: "震度6強",
    70: "震度7"
}

# 気象庁 警報コード変換 (気象庁防災情報XML コード管理表 令和8年最新仕様準拠)
WARNING_CODE_MAP = {
    # 警報 (Level 3 / 単独警報)
    "02": "暴風雪警報",
    "2": "暴風雪警報",
    "03": "大雨警報",
    "3": "大雨警報",
    "04": "洪水警報",
    "4": "洪水警報",
    "05": "暴風警報",
    "5": "暴風警報",
    "06": "大雪警報",
    "6": "大雪警報",
    "07": "波浪警報",
    "7": "波浪警報",
    "08": "高潮警報",
    "8": "高潮警報",
    "09": "土砂災害警報",
    "9": "土砂災害警報",
    # 特別警報 (Level 5 / 特別警報報知)
    "32": "暴風雪特別警報",
    "33": "大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "高潮特別警報",
    "39": "土砂災害特別警報",
    # 危険警報 (Level 4 / 令和8年新体系)
    "43": "大雨危険警報",
    "48": "高潮危険警報",
}

TSUNAMI_MAP = {
    "None": "この地震による津波の心配はありません。",
    "Unknown": "津波の有無は現在調査中です。",
    "Checking": "津波の有無を現在調査中です。",
    "NonEffective": "若干の海面変動が予想されますが、被害の心配はありません。",
    "Watch": "津波注意報が発表されています。海岸や河口付近に近づかないでください。",
    "Warning": "大津波警報または津波警報が発表されています。直ちに命を守る行動をとってください。"
}


def format_scale_name(scale_val: int) -> str:
    """震度数値を人間・TTS向け文字列に変換"""
    return SCALE_MAP.get(scale_val, f"震度{scale_val}")


def parse_earthquake_event(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """P2P地震情報 (code: 551) を解析"""
    eq = data.get("earthquake", {})
    if not eq:
        return None

    max_scale = eq.get("maxScale", 0)
    # 震源に関する情報 (Destination: maxScale=-1) など震度が未定の場合はスキップ
    if max_scale <= 0:
        return None

    hypocenter = eq.get("hypocenter", {})
    name = (hypocenter.get("name") or "").strip()
    depth = hypocenter.get("depth", -1)
    depth_str = f"約{depth}km" if depth >= 0 else "ごく浅い"
    mag = hypocenter.get("magnitude", -1)
    mag_str = f"M{mag:.1f}" if mag >= 0 else "不明"
    time_str = eq.get("time", "")
    tsunami_code = eq.get("domesticTsunami", "Unknown")
    tsunami_desc = TSUNAMI_MAP.get(tsunami_code, "")

    scale_name = format_scale_name(max_scale)

    # 被害の大きい都道府県の集計
    points = data.get("points", [])
    pref_scales: Dict[str, int] = {}
    for p in points:
        pref = p.get("pref", "")
        sc = p.get("scale", 0)
        if pref and sc > pref_scales.get(pref, 0):
            pref_scales[pref] = sc

    sorted_prefs = sorted(pref_scales.items(), key=lambda x: x[1], reverse=True)
    pref_summary = ", ".join([f"{p}: {format_scale_name(s)}" for p, s in sorted_prefs[:5]])
    pref_names = [p for p, _ in sorted_prefs[:3]]
    areas_str = "、".join(pref_names) + ("など" if len(sorted_prefs) > 1 else "")

    issue_type = data.get("issue", {}).get("type", "")

    # 震度速報 (ScalePrompt) または 震源地名が未定の場合
    if not name or issue_type == "ScalePrompt":
        title = f"🚨 【震度速報】{areas_str}で最大{scale_name}" if areas_str else f"🚨 【震度速報】各地で最大{scale_name}"
        voice_text = (
            f"【地震速報】各地で最大{scale_name}の揺れが観測されました。"
            f"{'揺れの大きかった地域は、' + areas_str + 'です。' if areas_str else ''}"
            f"今後の情報にご注意ください。"
        )
    else:
        # 各地の震度に関する情報 (DetailScale)
        title = f"🚨 【地震情報】{name}で最大{scale_name} ({mag_str})"
        voice_text = (
            f"【地震情報】{name}を震源とする最大{scale_name}の地震が発生しました。"
            f"深さは{depth_str}、マグニチュードは{mag_str}です。"
            f"{tsunami_desc}"
        )

    return {
        "type": "earthquake",
        "id": data.get("id", f"eq_{time_str}_{name}_{max_scale}"),
        "time": time_str,
        "max_scale": max_scale,
        "scale_name": scale_name,
        "hypocenter": name or "各地",
        "depth": depth_str,
        "magnitude": mag_str,
        "tsunami_desc": tsunami_desc,
        "voice_text": voice_text,
        "title": title,
        "pref_summary": pref_summary,
        "affected_prefs": list(pref_scales.keys())
    }


def parse_eew_event(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """P2P地震情報 (code: 556 または 554) 緊急地震速報を解析"""
    code = data.get("code")
    time_str = data.get("time", "")
    is_cancelled = bool(data.get("cancelled", False))
    issue = data.get("issue") or {}
    event_id = issue.get("eventId")
    serial = str(issue.get("serial", ""))

    # 1. 取消報の処理 (code 556 かつ cancelled: true)
    if is_cancelled:
        voice_text = "【緊急地震速報 取消】先ほどの緊急地震速報は取り消されました。揺れの心配はありません。"
        title = "ℹ️ 【緊急地震速報 (取消)】先ほどの速報は取り消されました"
        return {
            "type": "eew",
            "id": data.get("id", f"eew_{time_str}_{code}"),
            "event_id": event_id,
            "serial": serial,
            "time": time_str,
            "voice_text": voice_text,
            "title": title,
            "is_alert": False,
            "is_cancelled": True,
            "hypocenter": None,
            "magnitude": None,
            "depth": None,
            "target_prefs": [],
            "affected_prefs": []
        }

    # 2. 警報詳細の処理 (code 556 かつ cancelled: false)
    if code == 556:
        earthquake = data.get("earthquake") or {}
        hypocenter = earthquake.get("hypocenter") or {}
        hypo_name = (hypocenter.get("name") or "").strip()
        mag = hypocenter.get("magnitude")
        depth = hypocenter.get("depth")
        areas = data.get("areas") or []

        # 警戒対象の都道府県リスト (重複排除・出現順)
        target_prefs = list(dict.fromkeys(a.get("pref") for a in areas if a.get("pref")))

        # 読み上げ文章の生成（速報性を最優先し、冗長にならないよう簡潔に要点を伝える）
        if hypo_name:
            if target_prefs:
                if len(target_prefs) <= 3:
                    prefs_str = "、".join(target_prefs)
                else:
                    prefs_str = f"{'、'.join(target_prefs[:3])}など"
                voice_text = f"【緊急地震速報】{hypo_name}で地震。{prefs_str}は強い揺れに警戒してください。"
            else:
                voice_text = f"【緊急地震速報】{hypo_name}で地震。強い揺れに警戒してください。身を守る行動をとってください。"
            title = f"⚠️ 【緊急地震速報 (警報)】{hypo_name}で地震 強い揺れに警戒！"
        else:
            voice_text = "【緊急地震速報】緊急地震速報です。強い揺れに警戒してください。身を守る行動をとってください。"
            title = "⚠️ 【緊急地震速報 (警報)】強い揺れに警戒してください！"

        mag_str = f"M{mag:.1f}" if isinstance(mag, (int, float)) and mag > 0 else "不明"
        depth_str = f"約{depth}km" if isinstance(depth, (int, float)) and depth >= 0 else "不明"

        return {
            "type": "eew",
            "id": data.get("id", f"eew_{time_str}_{code}"),
            "event_id": event_id,
            "serial": serial,
            "time": time_str,
            "voice_text": voice_text,
            "title": title,
            "is_alert": True,
            "is_cancelled": False,
            "hypocenter": hypo_name or "各地",
            "magnitude": mag_str,
            "depth": depth_str,
            "target_prefs": target_prefs,
            "affected_prefs": target_prefs
        }

    # 3. 発表検知の処理 (code 554)
    voice_text = "【緊急地震速報】気象庁より緊急地震速報が発表されました。今後の揺れにご注意ください。"
    title = "⚠️ 【緊急地震速報】緊急地震速報が発表されました"

    return {
        "type": "eew",
        "id": data.get("id", f"eew_{time_str}_{code}"),
        "event_id": event_id,
        "serial": serial,
        "time": time_str,
        "voice_text": voice_text,
        "title": title,
        "is_alert": False,
        "is_cancelled": False,
        "hypocenter": None,
        "magnitude": None,
        "depth": None,
        "target_prefs": [],
        "affected_prefs": []
    }


def parse_tsunami_event(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """P2P地震情報 (code: 552) 津波予報を解析"""
    time_str = data.get("time", "")
    areas = data.get("areas", [])
    if not areas:
        return None

    # 各エリアの警報種別
    grades = []
    area_names = []
    has_major = False
    has_warning = False

    for a in areas:
        grade = a.get("grade", "")
        name = a.get("name", "")
        if "MajorWarning" in grade or "大津波" in grade:
            has_major = True
            grades.append("大津波警報")
        elif "Warning" in grade or "警報" in grade:
            has_warning = True
            grades.append("津波警報")
        elif "Watch" in grade or "注意報" in grade:
            grades.append("津波注意報")
        area_names.append(name)

    if has_major:
        top_grade = "大津波警報"
        voice_text = (
            "【大津波警報】大津波警報が発表されました！沿岸部や川沿いの方は、"
            "直ちに高台や避難ビルなど安全な場所へ避難してください！"
        )
    elif has_warning:
        top_grade = "津波警報"
        voice_text = (
            "【津波警報】津波警報が発表されました。海沿いの方は、直ちに高台へ避難してください。"
        )
    else:
        top_grade = "津波注意報"
        voice_text = (
            "【津波注意報】津波注意報が発表されました。海岸や河口付近から離れてください。"
        )

    title = f"🌊 【{top_grade}】発表 ({', '.join(area_names[:4])}等)"

    return {
        "type": "tsunami",
        "id": data.get("id", f"tsunami_{time_str}_{top_grade}"),
        "time": time_str,
        "top_grade": top_grade,
        "areas": area_names,
        "voice_text": voice_text,
        "title": title
    }


def parse_weather_warning_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """気象庁 map.json の単一要素から警報・特別警報の発表を抽出"""
    info_type = item.get("infoType", "")
    headline = item.get("headlineText", "")
    office = item.get("publishingOffice", "")
    report_time = item.get("reportDatetime", "")

    w = item.get("warning", {})
    c10_items = w.get("class10Items", [])

    new_special_warnings = []
    new_warnings = []

    for c10 in c10_items:
        for k in c10.get("kinds", []):
            code = str(k.get("code", "")).strip()
            status = k.get("status", "")
            # 「発表」または「継続」のうち新規発表を重視
            if status == "発表":
                name = WARNING_CODE_MAP.get(code) or WARNING_CODE_MAP.get(code.zfill(2))
                if not name:
                    continue
                if "特別警報" in name:
                    new_special_warnings.append(name)
                elif "警報" in name:
                    new_warnings.append(name)

    # 警報または特別警報の新規発表がない場合は通知対象外
    if not new_special_warnings and not new_warnings:
        return None

    # 重複除去
    new_special_warnings = sorted(list(set(new_special_warnings)))
    new_warnings = sorted(list(set(new_warnings)))

    is_special = len(new_special_warnings) > 0
    all_warnings = new_special_warnings + new_warnings
    warning_names_str = "、".join(all_warnings[:4])

    pref_name = office.replace("地方気象台", "地方").replace("気象庁", "東京地方")

    if is_special:
        title = f"🚨 【気象特別警報】{pref_name}に{warning_names_str}が発表されました"
        voice_text = (
            f"【気象特別警報】{pref_name}に、{warning_names_str}が発表されました。"
            f"ただちに命を守る最善の行動をとってください。{headline}"
        )
    else:
        title = f"⚠️ 【気象警報】{pref_name}に{warning_names_str}が発表されました"
        voice_text = f"【気象警報】{pref_name}に{warning_names_str}が発表されました。{headline}"

    event_id = f"weather_{report_time}_{office}_{'_'.join(all_warnings)}"

    return {
        "type": "weather",
        "id": event_id,
        "time": report_time,
        "office": office,
        "pref_name": pref_name,
        "is_special": is_special,
        "warnings": all_warnings,
        "headline": headline,
        "voice_text": voice_text,
        "title": title
    }
