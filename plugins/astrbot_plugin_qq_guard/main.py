"""qq_guard：QQ 群白名单、按用户限流、自消息防护与状态查询。

只在“已唤醒”的消息上生效（私聊、群内 @ 机器人、回复机器人、唤醒前缀指令），
普通群聊消息不会经过本插件，也不会消耗配额。
AstrBot 内置的会话白名单 / 会话级限流依旧有效，本插件在其后补充：
- 群白名单只限制群聊，不影响私聊；
- 限流按发送者计算，避免一个人耗尽整个群的会话配额。
"""

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter

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
        logger.info(
            "[qq_guard] 已加载：群白名单 %s 个，按用户限流 %s 次/%s 秒",
            len(self.guard.config.group_allowlist),
            self.guard.config.user_rate_limit_count or "不限",
            self.guard.config.user_rate_limit_window,
        )

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
        if remaining is None:
            lines.append("按用户限流: 未启用")
        else:
            lines.append(
                f"按用户限流: {cfg.user_rate_limit_count} 次/{cfg.user_rate_limit_window} 秒，剩余 {remaining}"
            )
        yield event.plain_result("\n".join(lines))
