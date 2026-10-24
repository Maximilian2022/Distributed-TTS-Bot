import unittest
from src.disaster.parser import (
    SCALE_MAP,
    WARNING_CODE_MAP,
    TSUNAMI_MAP,
    format_scale_name,
    parse_earthquake_event,
    parse_eew_event,
    parse_tsunami_event,
    parse_weather_warning_item
)


class TestDisasterParser(unittest.TestCase):
    def test_scale_formatting(self):
        """震度スケール変換テスト"""
        self.assertEqual(format_scale_name(10), "震度1")
        self.assertEqual(format_scale_name(40), "震度4")
        self.assertEqual(format_scale_name(45), "震度5弱")
        self.assertEqual(format_scale_name(46), "震度5弱以上と推定")
        self.assertEqual(format_scale_name(50), "震度5強")
        self.assertEqual(format_scale_name(55), "震度6弱")
        self.assertEqual(format_scale_name(60), "震度6強")
        self.assertEqual(format_scale_name(70), "震度7")
        self.assertEqual(format_scale_name(99), "震度99")

    def test_parse_earthquake_event(self):
        """P2P地震情報 (code 551) の解析テスト"""
        data = {
            "code": 551,
            "time": "2026/10/24 21:00:00",
            "earthquake": {
                "time": "2026/10/24 21:00:00",
                "maxScale": 50,
                "domesticTsunami": "None",
                "hypocenter": {
                    "name": "東京湾",
                    "depth": 30,
                    "magnitude": 5.2
                }
            },
            "points": [
                {"pref": "東京都", "scale": 50},
                {"pref": "神奈川県", "scale": 40},
                {"pref": "千葉県", "scale": 30}
            ]
        }
        res = parse_earthquake_event(data)
        self.assertIsNotNone(res)
        self.assertEqual(res["max_scale"], 50)
        self.assertEqual(res["scale_name"], "震度5強")
        self.assertEqual(res["hypocenter"], "東京湾")
        self.assertEqual(res["magnitude"], "M5.2")
        self.assertEqual(res["depth"], "約30km")
        self.assertIn("東京湾を震源とする最大震度5強の地震が発生しました", res["voice_text"])
        self.assertIn("津波の心配はありません", res["voice_text"])
        self.assertIn("東京都", res["pref_summary"])

    def test_parse_earthquake_skip_zero_scale(self):
        """震度未定 (maxScale <= 0) 時のスキップテスト"""
        data = {
            "code": 551,
            "earthquake": {
                "maxScale": -1,
                "hypocenter": {"name": "遠地"}
            }
        }
        self.assertIsNone(parse_earthquake_event(data))

    def test_parse_eew_alert_code_556(self):
        """緊急地震速報 (code 556 警報詳細) の解析テスト"""
        data = {
            "code": 556,
            "time": "2026/10/24 21:10:00",
            "cancelled": False,
            "issue": {
                "eventId": "20261024211000",
                "serial": 2
            },
            "earthquake": {
                "hypocenter": {
                    "name": "千葉県北西部",
                    "magnitude": 6.1,
                    "depth": 40
                }
            },
            "areas": [
                {"pref": "千葉県"},
                {"pref": "東京都"},
                {"pref": "埼玉県"}
            ]
        }
        res = parse_eew_event(data)
        self.assertIsNotNone(res)
        self.assertFalse(res["is_cancelled"])
        self.assertTrue(res["is_alert"])
        self.assertEqual(res["hypocenter"], "千葉県北西部")
        self.assertEqual(res["magnitude"], "M6.1")
        self.assertEqual(res["depth"], "約40km")
        self.assertIn("千葉県北西部で地震", res["voice_text"])
        self.assertIn("強い揺れに警戒してください", res["voice_text"])
        self.assertIn("千葉県、東京都、埼玉県", res["voice_text"])

    def test_parse_eew_cancelled(self):
        """緊急地震速報 取消報 (code 556, cancelled: true) の解析テスト"""
        data = {
            "code": 556,
            "time": "2026/10/24 21:15:00",
            "cancelled": True,
            "issue": {
                "eventId": "20261024211000",
                "serial": 3
            }
        }
        res = parse_eew_event(data)
        self.assertIsNotNone(res)
        self.assertTrue(res["is_cancelled"])
        self.assertFalse(res["is_alert"])
        self.assertIn("【緊急地震速報 取消】", res["voice_text"])
        self.assertIn("先ほどの緊急地震速報は取り消されました", res["voice_text"])
        self.assertIn("揺れの心配はありません", res["voice_text"])

    def test_parse_eew_detection_code_554(self):
        """緊急地震速報 検出報 (code 554) の解析テスト"""
        data = {
            "code": 554,
            "time": "2026/10/24 21:20:00"
        }
        res = parse_eew_event(data)
        self.assertIsNotNone(res)
        self.assertFalse(res["is_cancelled"])
        self.assertIn("緊急地震速報が発表されました", res["voice_text"])

    def test_parse_tsunami_event(self):
        """津波予報 (code 552) の解析テスト"""
        data = {
            "code": 552,
            "time": "2026/10/24 21:22:00",
            "areas": [
                {"name": "岩手県", "grade": "MajorWarning"},
                {"name": "宮城県", "grade": "Warning"}
            ]
        }
        res = parse_tsunami_event(data)
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "tsunami")
        self.assertEqual(res["top_grade"], "大津波警報")
        self.assertIn("大津波警報が発表されました", res["voice_text"])

    def test_parse_weather_warning_item(self):
        """気象庁警報 (map.json 要素) の解析テスト"""
        item = {
            "publishingOffice": "東京管区気象台",
            "reportDatetime": "2026-10-24T21:25:00+09:00",
            "headlineText": "土砂災害に警戒してください。",
            "warning": {
                "class10Items": [
                    {
                        "kinds": [
                            {"code": "03", "status": "発表"},
                            {"code": "04", "status": "発表"}
                        ]
                    }
                ]
            }
        }
        res = parse_weather_warning_item(item)
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "weather")
        self.assertFalse(res["is_special"])
        self.assertIn("大雨警報", res["warnings"])
        self.assertIn("洪水警報", res["warnings"])
        self.assertIn("気象警報", res["voice_text"])


if __name__ == "__main__":
    unittest.main()
