from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fu_gm.config import DEFAULT_LLM_MODEL


@dataclass(frozen=True)
class TestLLMClientBundle:
    """显式测试模式下，各模型职责使用的客户端集合。

    生产入口不会自行创建这个对象。调用方必须明确声明 ``test_only``，
    避免本地测试传输被误接到 AstrBot 或长期运行服务。
    """

    core: Any
    expressor: Any
    npc_design: Any
    pacing: Any
    summarizer: Any
    player: Any
    model: str = DEFAULT_LLM_MODEL
    test_only: bool = True

    __test__ = False

    def __post_init__(self) -> None:
        if not self.test_only:
            raise ValueError("测试 LLM 客户端包必须显式启用 test_only。")
        missing = [
            name
            for name in (
                "core",
                "expressor",
                "npc_design",
                "pacing",
                "summarizer",
                "player",
            )
            if getattr(self, name, None) is None
        ]
        if missing:
            raise ValueError(
                "测试 LLM 客户端包缺少职责客户端：" + "、".join(missing)
            )

    @classmethod
    def shared(
        cls,
        client: Any,
        *,
        model: str = DEFAULT_LLM_MODEL,
    ) -> "TestLLMClientBundle":
        """让所有语言职责共享同一条可审计测试传输。"""

        return cls(
            core=client,
            expressor=client,
            npc_design=client,
            pacing=client,
            summarizer=client,
            player=client,
            model=str(model or DEFAULT_LLM_MODEL).strip(),
        )


def require_test_llm_bundle(bundle: Any | None) -> Any | None:
    """验证可选测试依赖，不接受未标注的任意客户端对象。"""

    if bundle is None:
        return None
    if not bool(getattr(bundle, "test_only", False)):
        raise ValueError("外部 LLM 客户端包只能在显式 test_only 模式下注入。")
    return bundle


__all__ = ["TestLLMClientBundle", "require_test_llm_bundle"]
