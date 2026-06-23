from __future__ import annotations

from fu_gm.llm_client import ChatMessage


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
) -> list[ChatMessage]:
    """构造缓存友好的 Chat messages。

    第一条 system 消息保持稳定；每轮变化的状态、记忆、人设和环境信息都
    进入后续 user 消息，尽量让供应商的前缀缓存命中更靠前、更稳定。
    """

    reminder_text = "\n\n".join(system_reminder(title, content) for title, content in (reminders or []))
    content = str(user_content).strip()
    if reminder_text:
        content = f"{reminder_text}\n\n{content}"
    return [
        ChatMessage(role="system", content=with_static_boundary(static_system_prompt)),
        ChatMessage(role="user", content=content),
    ]
