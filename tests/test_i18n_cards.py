"""Regression tests for the language layer and the card builders.

Run with:  python -m unittest discover -s tests -v
No third-party dependencies.
"""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import feishu, i18n, llm  # noqa: E402


def set_lang(lang):
    os.environ["AIW_CARD_LANG"] = lang
    importlib.reload(i18n)
    return i18n


EVENT = {
    "page": {"id": "1", "name": "Demo Page"},
    "author": {"id": "2", "name": "Alice"},
    "comment": {"id": "c1", "message": "What is the lead time?",
                "created_time": 1759900000,
                "permalink_url": "https://example.com/p/1"},
    "post": {"id": "p1", "status_type": "added_video",
             "permalink_url": "https://example.com/reel/1/"},
}
SUGGESTION_ZH_VALUES = {
    "intent": "询价", "stage": "热线索", "action": "回复",
    "reply": "交期 3-4 个月。",
    "self_check": {"invented_specifics": False, "answers_question": True,
                   "cites_only_kb": True},
}
SUGGESTION_EN_VALUES = {
    "intent": "quote", "stage": "hot", "action": "reply",
    "reply": "Lead time is 3-4 months.",
    "self_check": {"invented_specifics": False, "answers_question": True,
                   "cites_only_kb": True},
}
LEAD_DISPLAY = {"name": "Bob", "company": "ACME", "country": "US",
                "submitted": "2026-10-19 10:00", "contacts": "bob@acme.com",
                "leadgen_id": "L1"}
LEAD_ANALYSIS = {"summary": "Wants 500 units", "priority": "高",
                 "reason": "asked price", "missing": "voltage",
                 "suggested_reply": "您好 Bob"}


class TestLabelTables(unittest.TestCase):
    def test_en_and_zh_tables_have_same_keys(self):
        en, zh = set(i18n.LABELS["en"]), set(i18n.LABELS["zh"])
        self.assertEqual(en - zh, set(), "keys present only in en")
        self.assertEqual(zh - en, set(), "keys present only in zh")

    def test_legacy_chinese_enums_normalise_to_codes(self):
        self.assertEqual(i18n.code("stage", "热线索"), "hot")
        self.assertEqual(i18n.code("stage", "hot"), "hot")
        self.assertEqual(i18n.code("action", "回复"), "reply")
        self.assertEqual(i18n.code("intent", "询价"), "quote")
        self.assertEqual(i18n.code("priority", "高"), "high")

    def test_every_code_used_by_the_prompt_has_a_label(self):
        for kind, codes in (("intent", ["quote", "technical", "after_sales",
                                        "partnership", "spam", "other"]),
                            ("stage", ["cold", "warm", "hot", "not_target"]),
                            ("action", ["reply", "forward_to_sales", "ignore"])):
            table = {"intent": i18n._INTENT, "stage": i18n._STAGE,
                     "action": i18n._ACTION}[kind]
            for c in codes:
                self.assertIn(c, table["en"], "%s/%s missing in en" % (kind, c))
                self.assertIn(c, table["zh"], "%s/%s missing in zh" % (kind, c))


class TestReviewCard(unittest.TestCase):
    def setUp(self):
        self.i18n = set_lang("en")

    def tearDown(self):
        set_lang("en")

    def _texts(self, card):
        out = []
        for el in card["body"]["elements"]:
            if el.get("content"):
                out.append(el["content"])
        return "\n".join(out)

    def test_card_is_english_by_default(self):
        card = feishu.build_review_card(EVENT, SUGGESTION_EN_VALUES)
        body = self._texts(card)
        self.assertIn("Commenter", body)
        self.assertIn("Intent: Quote", body)
        self.assertNotIn("评论者", body)

    def test_chinese_legacy_values_render_in_chinese_mode(self):
        set_lang("zh")
        card = feishu.build_review_card(EVENT, SUGGESTION_ZH_VALUES)
        body = self._texts(card)
        self.assertIn("评论者", body)
        self.assertIn("客户阶段：热线索", body)

    def test_comment_timestamp_is_rendered(self):
        """Regression: datetime was not imported, so the time line silently vanished."""
        card = feishu.build_review_card(EVENT, SUGGESTION_EN_VALUES)
        self.assertRegex(self._texts(card), r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_header_colour_follows_stage(self):
        for stage, colour in (("hot", "red"), ("warm", "orange"),
                              ("cold", "blue"), ("not_target", "grey")):
            sug = dict(SUGGESTION_EN_VALUES, stage=stage)
            card = feishu.build_review_card(EVENT, sug)
            self.assertEqual(card["header"]["template"], colour, stage)

    def test_missing_self_check_keys_raise_no_false_alarms(self):
        sug = {"intent": "quote", "stage": "hot", "action": "reply",
               "reply": "ok", "self_check": {}}
        card = feishu.build_review_card(EVENT, sug)
        self.assertNotIn("⚠️", self._texts(card))

    def test_explicit_false_flags_are_reported(self):
        sug = {"intent": "quote", "stage": "hot", "action": "reply",
               "reply": "ok",
               "self_check": {"invented_specifics": True,
                              "answers_question": False,
                              "cites_only_kb": False}}
        card = feishu.build_review_card(EVENT, sug)
        self.assertEqual(self._texts(card).count("⚠️"), 3)


class TestLeadCard(unittest.TestCase):
    def setUp(self):
        set_lang("en")

    def test_high_priority_is_red(self):
        card = feishu.build_lead_card("Demo Page", LEAD_DISPLAY, LEAD_ANALYSIS, "")
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("High", json_text(card))

    def test_priority_falls_back_to_medium_on_unknown_value(self):
        ana = dict(LEAD_ANALYSIS, priority="???")
        card = feishu.build_lead_card("Demo Page", LEAD_DISPLAY, ana, "")
        self.assertEqual(card["header"]["template"], "orange")


def json_text(card):
    import json
    return json.dumps(card, ensure_ascii=False)


class TestLanguageDetection(unittest.TestCase):
    def test_chinese_comment(self):
        self.assertEqual(llm._detect_language("交期多久？"), "zh")

    def test_english_comment(self):
        self.assertEqual(llm._detect_language("What is the lead time?"), "en")

    def test_empty_comment(self):
        self.assertIsNone(llm._detect_language(""))

    def test_mostly_english_with_one_chinese_char(self):
        # A stray CJK character inside a mostly-English comment must not flip the language.
        self.assertEqual(llm._detect_language("Great product indeed 好"), "en")

    def test_short_bilingual_fragment_defaults_to_chinese(self):
        # "ok 好" is what a Chinese speaker types; too little signal to call it English.
        self.assertEqual(llm._detect_language("ok 好"), "zh")


if __name__ == "__main__":
    unittest.main()
