from __future__ import annotations

import unittest

import numpy as np

from chinese_speech.text import float_audio_to_pcm, normalize_tts_text, split_chinese_text


class ChineseTextTests(unittest.TestCase):
    def test_normalizes_markdown_url_and_date(self) -> None:
        value = normalize_tts_text(
            "请看 [文档](https://example.com)，日期 2026-08-31，`API`，价格￥12.5，完成80%。"
        )
        self.assertNotIn("https://", value)
        self.assertNotIn("`", value)
        self.assertIn("文档", value)
        self.assertIn("2026年8月31日", value)
        self.assertIn("12.5元", value)
        self.assertIn("百分之80", value)

    def test_split_prefers_complete_sentences_and_limits_chunks(self) -> None:
        text = "这是第一段比较完整的中文句子，用来确认能够尽快开始合成。" * 4
        chunks = split_chinese_text(text, 15, 80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_pcm_conversion_clips_and_fades_boundaries(self) -> None:
        pcm = float_audio_to_pcm(np.ones(2400, dtype=np.float32) * 2, sample_rate=24000)
        samples = np.frombuffer(pcm, dtype="<i2")
        self.assertEqual(samples[0], 0)
        self.assertEqual(samples[-1], 0)
        self.assertLessEqual(samples.max(), 32767)


if __name__ == "__main__":
    unittest.main()
