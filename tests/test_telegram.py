"""Focused checks for the optional Telegram streaming transport."""
from __future__ import annotations

import html
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leftover import render                                      # noqa: E402
from leftover.agents import Event                                # noqa: E402
from leftover.config import AgentSpec, Config                    # noqa: E402

try:
    from leftover.transports.telegram import Bot                 # noqa: E402
except ModuleNotFoundError as exc:
    if exc.name != "telegram":
        raise
    Bot = None  # type: ignore[misc,assignment]


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[str] = []

    async def send_message(self, _chat_id: int, text: str, **_kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=7)

    async def edit_message_text(self, text: str, **_kwargs) -> None:
        self.edited.append(text)


@unittest.skipUnless(Bot is not None, "python-telegram-bot is not installed")
class TelegramSinkTests(unittest.IsolatedAsyncioTestCase):
    def make_sink(self, interval: float):
        spec = AgentSpec(key="gpt", label="Codex", emoji="G")
        config = Config(agents=[spec], stream_edit_interval=interval)
        transport = Bot.__new__(Bot)
        transport.config = config
        fake = FakeTelegramBot()
        factory = transport.make_sink(42, SimpleNamespace(bot=fake))
        return spec, fake, factory

    async def test_thought_is_not_rendered_as_answer_metadata(self) -> None:
        spec, fake, factory = self.make_sink(float("inf"))
        on_event = await factory(spec)

        await on_event(Event("thought", "private reasoning <do not show>"))
        self.assertEqual(fake.edited, [])
        await on_event(Event("text", "public answer"))
        await on_event(Event("done", ""))

        self.assertEqual(len(fake.edited), 1)
        final = fake.edited[0]
        self.assertNotIn("private reasoning", final)
        self.assertEqual(final, "<b>G Codex</b>\npublic answer")

    async def test_status_and_answer_fit_complete_html_messages(self) -> None:
        spec, fake, factory = self.make_sink(0.0)
        on_event = await factory(spec)
        status = "  member\n\t" + "<&> status   " * 30
        answer = "<&>" * 3000

        await on_event(Event("status", status))
        await on_event(Event("text", answer))
        self.assertEqual(len(fake.sent), 1)
        await on_event(Event("done", ""))

        messages = [fake.edited[-1], *fake.sent[1:]]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(
            len(message.encode("utf-16-le")) // 2 <= render.TELEGRAM_LIMIT
            for message in messages
        ))
        for message in messages:
            ET.fromstring("<root>" + message + "</root>")

        header, first_body = messages[0].split("\n", 1)
        parsed_header = ET.fromstring("<root>" + header + "</root>")
        note = parsed_header.findtext("i") or ""
        self.assertLessEqual(len(note), 120)
        self.assertNotIn("\n", note)
        self.assertNotIn("  ", note)
        self.assertTrue(note.endswith("..."))
        rendered_body = first_body + "".join(messages[1:])
        self.assertEqual(html.unescape(rendered_body), answer)

    async def test_long_fenced_non_bmp_answer_round_trips_visible_text(self) -> None:
        spec, fake, factory = self.make_sink(float("inf"))
        on_event = await factory(spec)
        code = "<&>\U0001f600" * 2400 + "\n"
        answer = f"```python\n{code}```"

        await on_event(Event("text", answer))
        await on_event(Event("done", ""))

        messages = [fake.edited[-1], *fake.sent[1:]]
        self.assertGreater(len(messages), 1)
        bodies = [messages[0].split("\n", 1)[1], *messages[1:]]
        for message, body in zip(messages, bodies):
            self.assertLessEqual(
                len(message.encode("utf-16-le")) // 2,
                render.TELEGRAM_LIMIT,
            )
            ET.fromstring("<root>" + message + "</root>")
            self.assertTrue(body.startswith("<pre><code"), body[:80])
            self.assertTrue(body.endswith("</code></pre>"), body[-80:])

        def visible(fragment: str) -> str:
            root = ET.fromstring("<root>" + fragment + "</root>")
            return "".join(root.itertext())

        expected = visible(render.to_html(answer))
        actual = "".join(visible(body) for body in bodies)
        self.assertEqual(actual, expected)
        self.assertNotIn("python", actual)
        self.assertEqual(actual.count("\n"), expected.count("\n"))


if __name__ == "__main__":
    unittest.main()
