"""qq_guard：QQ 群白名单、按用户限流、自消息防护、群聊自然接话与状态查询。

只在“已唤醒”的消息上生效（私聊、群内 @ 机器人、回复机器人、唤醒前缀指令），
普通群聊消息不会经过本插件，也不会消耗配额。
AstrBot 内置的会话白名单 / 会话级限流依旧有效，本插件在其后补充：
- 群白名单只限制群聊，不影响私聊；
- 限流按发送者计算，避免一个人耗尽整个群的会话配额。

群聊自然接话（natural_chat_groups 中的群）：机器人旁听群聊，在合适的时机不需要 @ 也会接话。
真正的回复走 AstrBot 的正常对话流程（使用你的人格设定与会话历史，并由内置的群聊上下文
功能注入最近的群消息）；本插件只决定“何时说话”，不修改人格提示词。
"""

import asyncio
import time

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.platform import MessageType

from .chatter import (
    DECISION_SYSTEM_PROMPT,
    Chatter,
    ChatterConfig,
    build_decision_prompt,
    build_reply_prompt,
    parse_decision,
)
from .guard import Guard, GuardConfig, MessageInfo, Verdict


class WokenOnly(CustomFilter):
    """只匹配已被唤醒的消息，避免普通群聊消息进入后续流水线。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:  # noqa: ANN001
        return bool(event.is_at_or_wake_command)


class QQGuard(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.guard = Guard(GuardConfig.from_mapping(dict(config or {})))
        self.chatter = Chatter(ChatterConfig.from_mapping(dict(config or {})))
        logger.info(
            "[qq_guard] 已加载：群白名单 %s 个，按用户限流 %s 次/%s 秒，自然接话群 %s 个",
            len(self.guard.config.group_allowlist),
            self.guard.config.user_rate_limit_count or "不限",
            self.guard.config.user_rate_limit_window,
            len(self.chatter.config.groups),
        )

    def _chat_enabled(self, group_id: str) -> bool:
        return self.chatter.enabled_for(group_id) and self.guard.group_allowed(group_id)

    @staticmethod
    def _info(event: AstrMessageEvent) -> MessageInfo:
        return MessageInfo(
            sender_id=str(event.get_sender_id() or ""),
            self_id=str(event.get_self_id() or ""),
            group_id=str(event.get_group_id() or ""),
            is_admin=event.is_admin(),
        )

    @filter.custom_filter(WokenOnly)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def gate(self, event: AstrMessageEvent):
        info = self._info(event)
        verdict = self.guard.check(info)
        if verdict is Verdict.ALLOW:
            return

        # 阻止默认 LLM 请求和后续插件处理
        event.should_call_llm(True)
        event.stop_event()
        if verdict is Verdict.SELF_MESSAGE:
            logger.debug("[qq_guard] 忽略机器人自己的消息")
        elif verdict is Verdict.GROUP_NOT_ALLOWED:
            logger.info("[qq_guard] 群 %s 不在白名单，已忽略", info.group_id)
        elif verdict is Verdict.RATE_LIMITED:
            logger.info("[qq_guard] 用户 %s 触发限流", info.sender_id)
            if self.guard.should_notify(info.sender_id):
                wait = int(self.guard.limiter.retry_after(info.sender_id)) + 1
                yield event.plain_result(f"消息太频繁啦，请 {wait} 秒后再试。")

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL, priority=-10)
    async def natural_chat(self, event: AstrMessageEvent):
        """群聊自然接话：停顿后判断是否有自然的接话点，有则按正常对话流程回复。"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        group_id = str(event.get_group_id() or "")
        if not self._chat_enabled(group_id):
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        text = event.message_str.strip() or "[图片/表情]"
        seq = self.chatter.observe(group_id, event.get_sender_name() or str(event.get_sender_id()), text)
        if event.is_at_or_wake_command:
            return  # 被 @ 或指令：走正常流程，机器人的回复会在 after_message_sent 中记录

        await asyncio.sleep(self.chatter.config.quiet_seconds)
        if not self.chatter.should_evaluate(group_id, seq):
            return

        self.chatter.begin(group_id)
        replied = False
        try:
            umo = event.unified_msg_origin
            provider = await self.context.get_using_provider_async(umo)
            if not provider:
                return
            started = time.monotonic()
            decision = await provider.text_chat(
                prompt=build_decision_prompt(self._bot_name(event), self.chatter.transcript(group_id)),
                system_prompt=DECISION_SYSTEM_PROMPT,
            )
            answer = (decision.completion_text or "").strip()
            elapsed = time.monotonic() - started
            if not parse_decision(answer):
                logger.info("[qq_guard] 群 %s 自然接话判断：不接话（%s，%.1fs）", group_id, answer[:20] or "空", elapsed)
                return
            logger.info("[qq_guard] 群 %s 自然接话判断：接话（%.1fs）", group_id, elapsed)
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            conversation = await conv_mgr.get_conversation(umo, cid)
            # 多条消息几乎同时到达时 AstrBot 会并发处理，触发判断的未必是最后一条，
            # 所以回复提示使用本插件按顺序记录的群聊内容，而不是“触发这次判断的那条消息”；
            # 同时让内置群聊上下文本次不再注入（避免重复），并在发送后清空它已记录的内容。
            event.set_extra("_group_context_record_id", None)
            event.set_extra("_group_context_raw_idx", -1)
            event.set_extra("_clean_group_context_session", True)
            prompt = build_reply_prompt(self.chatter.context_for_reply(group_id))
            yield event.request_llm(prompt=prompt, conversation=conversation)
            replied = True
        except Exception as e:
            logger.error("[qq_guard] 群 %s 自然接话失败：%s", group_id, e)
        finally:
            self.chatter.end(group_id, replied)

    @filter.after_message_sent()
    async def remember_bot_message(self, event: AstrMessageEvent):
        """记录机器人在群里的发言，用于冷却与接话判断的上下文。"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        group_id = str(event.get_group_id() or "")
        if not self._chat_enabled(group_id):
            return
        result = event.get_result()
        text = result.get_plain_text() if result else ""
        if text.strip():
            self.chatter.observe_bot(group_id, text.strip())

    def _bot_name(self, event: AstrMessageEvent) -> str:
        triggers = self.chatter.config.name_triggers
        return triggers[0] if triggers else ""

    @filter.command("guard")
    async def status(self, event: AstrMessageEvent):
        """查看当前会话的 qq_guard 状态（群号、白名单、剩余配额）"""
        info = self._info(event)
        cfg = self.guard.config
        remaining = self.guard.limiter.remaining(info.sender_id)
        lines = [
            "[qq_guard 状态]",
            f"会话: {event.unified_msg_origin}",
            f"你的 QQ: {info.sender_id}" + ("（管理员）" if info.is_admin else ""),
        ]
        if info.group_id:
            allow = "是" if self.guard.group_allowed(info.group_id) else "否"
            scope = "未启用（所有群可用）" if not cfg.group_allowlist else f"{len(cfg.group_allowlist)} 个群"
            lines += [f"群号: {info.group_id}", f"群白名单: {scope}；本群允许: {allow}"]
            chat = "开启" if self._chat_enabled(info.group_id) else "关闭"
            lines.append(f"自然接话: {chat}")
        if remaining is None:
            lines.append("按用户限流: 未启用")
        else:
            lines.append(
                f"按用户限流: {cfg.user_rate_limit_count} 次/{cfg.user_rate_limit_window} 秒，剩余 {remaining}"
            )
        yield event.plain_result("\n".join(lines))
