"""Telegram front end - the thing that makes this feel like a group chat."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from html.parser import HTMLParser
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import doctor, render
from ..agents import AgentPool, Event
from ..config import AgentSpec, Config
from ..orchestrator import Orchestrator, summarise
from ..router import Router

log = logging.getLogger("leftover.telegram")

_STATUS_LIMIT = 120


def _compact_note(text: str, limit: int = _STATUS_LIMIT) -> str:
    """Keep progress metadata short, single-line, and safe for a header."""
    note = " ".join(text.split())
    if len(note) <= limit:
        return note
    return note[:limit - 3].rstrip() + "..."


def _utf16_units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


class _HTMLSegments(HTMLParser):
    """Collect visible HTML tokens with the formatting active around them."""

    _ALLOWED = {"b", "i", "code", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[tuple[str, str]] = []
        self.segments: list[
            tuple[tuple[tuple[str, str], ...], list[str]]
        ] = []

    @staticmethod
    def _opening(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if tag == "code":
            language = next(
                (value for name, value in attrs
                 if name == "class" and value and value.startswith("language-")),
                "",
            )
            if language and _utf16_units(language) <= 80:
                return f'<code class="{html.escape(language, quote=True)}">'
        return f"<{tag}>"

    def _append(self, tokens: list[str]) -> None:
        if not tokens:
            return
        wrappers = tuple(self.stack)
        if self.segments and self.segments[-1][0] == wrappers:
            self.segments[-1][1].extend(tokens)
        else:
            self.segments.append((wrappers, tokens))

    def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ALLOWED:
            self.stack.append((tag, self._opening(tag, attrs)))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._append(list(data))

    def handle_entityref(self, name: str) -> None:
        self._append([f"&{name};"])

    def handle_charref(self, name: str) -> None:
        self._append([f"&#{name};"])


def _split_rendered_html(rendered: str, first_limit: int) -> list[str]:
    """Split rendered HTML while closing every tag in every message."""
    parser = _HTMLSegments()
    parser.feed(rendered)
    parser.close()

    chunks: list[str] = []
    current: list[str] = []
    current_units = 0
    limit = first_limit

    def finish(*, allow_empty: bool = False) -> None:
        nonlocal current, current_units, limit
        if current or allow_empty:
            chunks.append("".join(current))
        current = []
        current_units = 0
        limit = render.TELEGRAM_LIMIT

    for wrappers, tokens in parser.segments:
        opening = "".join(value for _tag, value in wrappers)
        closing = "".join(f"</{tag}>" for tag, _value in reversed(wrappers))
        wrapper_units = _utf16_units(opening) + _utf16_units(closing)
        index = 0
        while index < len(tokens):
            available = limit - current_units - wrapper_units
            if available <= 0:
                if not current and limit == render.TELEGRAM_LIMIT:
                    raise ValueError("Telegram HTML wrapper exceeds the message limit")
                finish(allow_empty=not chunks and not current)
                continue

            start = index
            token_units = 0
            while index < len(tokens):
                units = _utf16_units(tokens[index])
                if token_units + units > available:
                    break
                token_units += units
                index += 1

            if index == start:
                if not current and limit == render.TELEGRAM_LIMIT:
                    raise ValueError("Telegram HTML token exceeds the message limit")
                finish(allow_empty=not chunks and not current)
                continue

            current.extend((opening, "".join(tokens[start:index]), closing))
            current_units += wrapper_units + token_units
            if index < len(tokens):
                finish()

    if current:
        finish()
    if not chunks:
        chunks.append("")
    return chunks


def _telegram_messages(head: str, body: str) -> list[str]:
    """Render complete HTML messages without cutting tags or entities."""
    rendered = render.to_html(body)
    prefix = head + "\n"
    if _utf16_units(prefix + rendered) <= render.TELEGRAM_LIMIT:
        return [prefix + rendered]
    first_limit = render.TELEGRAM_LIMIT - _utf16_units(prefix)
    if first_limit < 0:
        raise ValueError("Telegram header exceeds the message limit")
    pieces = _split_rendered_html(rendered, first_limit)
    messages = [prefix + pieces[0], *pieces[1:]]
    if not all(_utf16_units(message) <= render.TELEGRAM_LIMIT
               for message in messages):
        raise ValueError("Telegram message exceeds the message limit")
    return messages


HELP = """<b>agora</b> - your local CLI agents, in this chat.

<b>Talk to one</b>
  @claude / @gpt / @grok / @cursor  &lt;question&gt;

<b>Talk to several</b>
  /heavy &lt;question&gt;  independent takes, then compare-notes
  /rt &lt;question&gt;      roundtable, each sees the last answer
  /all &lt;question&gt;     everyone answers in parallel
  /debate &lt;claim&gt;     two argue, a third judges

<b>Heavy work</b>
  /cd &lt;path&gt;          set the working directory (a real repo)
  /relay &lt;task&gt;       plan -&gt; implement -&gt; review, with file access
  /stop               cancel whatever is running

<b>Routing</b>
  @any &lt;question&gt;     let the router pick whoever has headroom
  /quota              per-agent quota and cooldown state
  /strategy &lt;name&gt;    headroom | order | cheapest | sticky

<b>Housekeeping</b>
  /who  /pwd  /reset  /auto  /doctor

Agents that hit a limit are taken out of rotation until their reset time,
and the turn is retried on the next one automatically.
"""


class Bot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.pool = AgentPool(config)
        # Quota and health belong to the machine, so one router serves every chat.
        self.router = Router(config, self.pool)
        # one orchestrator per chat so transcripts do not bleed together
        self.chats: dict[int, Orchestrator] = {}
        self.jobs: dict[int, asyncio.Task] = {}

    # --- plumbing ------------------------------------------------------------

    def orch(self, chat_id: int) -> Orchestrator:
        if chat_id not in self.chats:
            self.chats[chat_id] = Orchestrator(self.config, self.pool, self.router)
        return self.chats[chat_id]

    def allowed(self, update: Update) -> bool:
        if not self.config.allowed_user_ids:
            return True
        user = update.effective_user
        return bool(user and user.id in self.config.allowed_user_ids)

    async def guard(self, update: Update) -> bool:
        if self.allowed(update):
            return True
        if update.effective_message:
            uid = update.effective_user.id if update.effective_user else "?"
            await update.effective_message.reply_text(
                f"Not on the allow-list. Your user id is {uid}.")
        return False

    # --- live-updating message sink -----------------------------------------

    def make_sink(self, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
        interval = self.config.stream_edit_interval

        async def sink(spec: AgentSpec):
            msg = await ctx.bot.send_message(
                chat_id, render.header(spec.emoji, spec.label, "thinking..."),
                parse_mode=ParseMode.HTML)
            state = {
                "buf": "", "tools": [], "status": "",
                "last": 0.0, "shown": "",
            }

            async def flush(final: bool = False) -> None:
                body = state["buf"].strip()
                tools = state["tools"][-3:]
                if final:
                    note = state["status"]
                else:
                    note = (tools[-1] if tools else
                            state["status"] or "thinking...")
                note = _compact_note(note)
                head = render.header(spec.emoji, spec.label, note)
                messages = _telegram_messages(
                    head, body or ("..." if not final else "(no output)"))
                text = messages[0]
                if text != state["shown"]:
                    state["shown"] = text
                    try:
                        await ctx.bot.edit_message_text(
                            text, chat_id=chat_id, message_id=msg.message_id,
                            parse_mode=ParseMode.HTML)
                    except BadRequest as exc:
                        if "not modified" not in str(exc).lower():
                            log.warning("edit failed: %s", exc)
                if final:
                    for extra in messages[1:]:
                        await ctx.bot.send_message(
                            chat_id, extra, parse_mode=ParseMode.HTML)

            async def on_event(ev: Event) -> None:
                if ev.kind == "text":
                    state["buf"] += ev.text
                elif ev.kind == "tool":
                    state["tools"].append(ev.text)
                elif ev.kind == "status":
                    state["status"] = _compact_note(ev.text)
                elif ev.kind == "thought":
                    return
                elif ev.kind == "error":
                    state["buf"] += f"\n\n[error] {ev.text}"
                elif ev.kind == "done":
                    await flush(final=True)
                    return
                now = time.monotonic()
                if now - state["last"] >= interval:
                    state["last"] = now
                    await flush()

            return on_event

        sink.leftover_turn_status = True  # type: ignore[attr-defined]
        return sink

    # --- handlers ------------------------------------------------------------

    async def on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update):
            await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def on_who(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        lines = []
        for a in self.config.agents:
            mark = "on " if (a.enabled and a.installed) else "off"
            names = "/".join(["@" + a.key, *("@" + x for x in a.aliases[:2])])
            lines.append(f"[{mark}] {a.emoji} {a.label} {names} - {a.tier}")
        await update.effective_message.reply_text("\n".join(lines))

    async def on_pwd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update):
            await update.effective_message.reply_text(f"workdir: {self.pool.workdir}")

    async def on_cd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        raw = " ".join(ctx.args or []).strip()
        target = Path(os.path.expanduser(raw or "~")).resolve()
        if not target.is_dir():
            await update.effective_message.reply_text(f"no such directory: {target}")
            return
        await self.pool.set_workdir(str(target))
        await update.effective_message.reply_text(
            f"workdir -> {target}\nagent sessions restarted here.")

    async def on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        self.orch(update.effective_chat.id).transcript.clear()
        await update.effective_message.reply_text("transcript cleared.")

    async def on_auto(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        arg = (ctx.args or ["toggle"])[0].lower()
        self.config.auto_reply = (
            not self.config.auto_reply if arg == "toggle" else arg in ("on", "true", "1"))
        await update.effective_message.reply_text(
            f"auto-reply without @mention: {'on' if self.config.auto_reply else 'off'}")

    async def on_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        chat_id = update.effective_chat.id
        task = self.jobs.get(chat_id)
        await self.pool.cancel_all()
        if task and not task.done():
            task.cancel()
        await update.effective_message.reply_text("cancelled.")

    async def on_quota(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        report = await self.router.report()
        await update.effective_message.reply_text(
            f"<pre>{html.escape(report)}</pre>", parse_mode=ParseMode.HTML)

    async def on_strategy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        choices = ("headroom", "order", "cheapest", "sticky")
        arg = (ctx.args or [""])[0].lower()
        if arg not in choices:
            await update.effective_message.reply_text(
                f"strategy is {self.config.routing.strategy}. "
                f"pick one of: {', '.join(choices)}")
            return
        self.config.routing.strategy = arg
        await update.effective_message.reply_text(f"strategy -> {arg}")

    async def on_doctor(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        report = await doctor.run(self.config)
        await update.effective_message.reply_text(
            f"<pre>{html.escape(report)}</pre>", parse_mode=ParseMode.HTML)

    async def on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        msg = update.effective_message
        text = (msg.text or msg.caption or "").strip()
        if not text:
            return
        chat = update.effective_chat
        orch = self.orch(chat.id)
        plan = orch.parse(text, in_group=chat.type in ("group", "supergroup"))
        if plan is None:
            if text.startswith("/"):
                await msg.reply_text("unknown command - /help")
            return
        if not plan.actionable:
            await msg.reply_text("give me something to work with.")
            return

        running = self.jobs.get(chat.id)
        if running and not running.done():
            await msg.reply_text("still working - /stop to cancel.")
            return

        await ctx.bot.send_chat_action(chat.id, ChatAction.TYPING)
        sink = self.make_sink(chat.id, ctx)

        async def job() -> None:
            started = time.monotonic()
            try:
                turns = await orch.execute(plan, sink)
            except asyncio.CancelledError:
                raise
            except Exception as exc:               # noqa: BLE001
                log.exception("run failed")
                await ctx.bot.send_message(chat.id, f"run failed: {exc}")
                return
            note = orch.last_decision.describe() if orch.last_decision else ""
            if plan.mode != "ask":
                await ctx.bot.send_message(
                    chat.id,
                    f"<i>{html.escape(plan.mode)} done in "
                    f"{time.monotonic() - started:.0f}s - "
                    f"{html.escape(summarise(turns))}</i>",
                    parse_mode=ParseMode.HTML)
            elif note:
                # A substitution happened - say so rather than silently
                # answering as somebody else.
                await ctx.bot.send_message(
                    chat.id, f"<i>routed: {html.escape(note)}</i>",
                    parse_mode=ParseMode.HTML)

        self.jobs[chat.id] = asyncio.create_task(job())

    # --- lifecycle -----------------------------------------------------------

    def build(self) -> Application:
        app = Application.builder().token(self.config.telegram_token).build()
        app.add_handler(CommandHandler(["start", "help"], self.on_help))
        app.add_handler(CommandHandler("who", self.on_who))
        app.add_handler(CommandHandler("pwd", self.on_pwd))
        app.add_handler(CommandHandler("cd", self.on_cd))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("auto", self.on_auto))
        app.add_handler(CommandHandler("stop", self.on_stop))
        app.add_handler(CommandHandler("doctor", self.on_doctor))
        app.add_handler(CommandHandler("quota", self.on_quota))
        app.add_handler(CommandHandler("strategy", self.on_strategy))
        app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL,
                                       self.on_message))
        return app


def main(config: Config) -> None:
    if not config.telegram_token:
        raise SystemExit(
            "no Telegram token - set LEFTOVER_TELEGRAM_TOKEN "
            "or [telegram].token")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bot = Bot(config)
    log.info("agora up - workdir %s, agents %s", bot.pool.workdir,
             ", ".join(a.label for a in config.enabled_agents()))
    bot.build().run_polling(drop_pending_updates=True)
