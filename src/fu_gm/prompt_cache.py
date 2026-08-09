from __future__ import annotations

from fu_gm.llm_client import ChatMessage


# Kept in the text for backwards-compatible prompt fingerprints and human
# diagnostics. The provider-visible cache boundary is ChatMessage.cache_breakpoint.
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "<!-- FU-GM SYSTEM_PROMPT_DYNAMIC_BOUNDARY -->"


def with_static_boundary(prompt: str) -> str:
    """给静态 system prompt 加上明确的动静分界标记。

    标记本身是稳定字节，不包含时间、路径、记忆或当前场景数据。
    """

    prompt = prompt.strip()
    if SYSTEM_PROMPT_DYNAMIC_BOUNDARY in prompt:
        return prompt
    return f"{prompt}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}"


def system_reminder(title: str, content: str) -> str:
    """把动态上下文包装成消息流里的 system-reminder。

    语义上它是系统级提醒，但物理位置放在 user message 前后，避免改动
    可缓存的静态 system prompt 前缀。
    """

    clean_title = " ".join(str(title).strip().split()) or "动态上下文"
    clean_content = str(content).strip()
    if not clean_content:
        clean_content = "无。"
    return f'<system-reminder title="{clean_title}">\n{clean_content}\n</system-reminder>'


def build_cache_friendly_messages(
    *,
    static_system_prompt: str,
    user_content: str,
    reminders: list[tuple[str, str]] | None = None,
    cache_family: str = "system",
    cache_breakpoint_offsets: tuple[int, ...] = (),
    user_cache_breakpoint_offsets: tuple[int, ...] = (),
) -> list[ChatMessage]:
    """构造缓存友好的 Chat messages。

    第一条 system 消息保持稳定；每轮变化的状态、记忆、人设和环境信息都
    进入后续 user 消息，尽量让供应商的前缀缓存命中更靠前、更稳定。
    """

    reminder_text = "\n\n".join(system_reminder(title, content) for title, content in (reminders or []))
    content = str(user_content).strip()
    user_offset_shift = 0
    if reminder_text:
        reminder_prefix = f"{reminder_text}\n\n"
        content = f"{reminder_prefix}{content}"
        user_offset_shift = len(reminder_prefix)
    system_content = with_static_boundary(static_system_prompt)
    offsets = tuple(
        sorted(
            {
                max(0, min(len(system_content), int(offset)))
                for offset in cache_breakpoint_offsets
                if int(offset) > 0
            }
        )
    )
    user_offsets = tuple(
        sorted(
            {
                max(0, min(len(content), int(offset) + user_offset_shift))
                for offset in user_cache_breakpoint_offsets
                if int(offset) > 0
            }
        )
    )
    return [
        ChatMessage(
            role="system",
            content=system_content,
            cache_breakpoint=True,
            cache_family=str(cache_family or "system"),
            cache_breakpoint_offsets=offsets,
        ),
        ChatMessage(
            role="user",
            content=content,
            cache_breakpoint=bool(user_offsets),
            cache_breakpoint_offsets=user_offsets,
        ),
    ]
