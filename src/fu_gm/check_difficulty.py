from __future__ import annotations


# Keep this compact rubric shared by the stable GM prompt and every tool where
# the GM chooses an open-check difficulty. The rules engine validates the
# submitted number, but never replaces the GM's judgment.
OPEN_CHECK_DIFFICULTY_RUBRIC = (
    "普通属性检定难度标尺：难度等级7为简单，适合任何受过训练或有天赋的人；"
    "难度等级10为正常，适合有相关能力的人或非常有才华的人；"
    "难度等级13为困难，通常需要专家或天才；"
    "难度等级16为非常困难，通常只有该领域最优秀的人才能做到。"
)

OPEN_CHECK_DIFFICULTY_GUIDANCE = (
    f"{OPEN_CHECK_DIFFICULTY_RUBRIC}"
    "先判断是否真的需要检定：若结果并不真正存在不确定性，或失败不会带来有意义的后果，"
    "就让行动自然成功。需要检定时，按当前障碍本身逐次独立裁定；"
    "不要根据角色的属性骰反向调整难度，也不要因为上一项检定用了某个难度等级就继续沿用。"
)
