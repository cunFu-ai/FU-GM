from __future__ import annotations

import re
from collections.abc import Iterable

from fu_gm.models import Clock


class ClockNarrativeBoundary:
    """Keep narrated pressure inside the progress authorized by a clock."""

    _ARRIVAL_CLOCK = re.compile(r"巡逻|追兵|车队|援军|增援|逼近|抵达|赶到|包围|封锁")
    _ARRIVAL_EVENT = re.compile(
        r"(?:他们|辉钢的人|财团的人|巡逻队|追兵|车队|援军|增援)[^。！？]{0,12}(?:已经|终于|此刻|现在)?(?:到了|抵达|赶到)"
        # Bare arrival verbs have no reliable subject.  Treating every
        # “抵达” as the threat arriving also blocks perfectly legal lines such
        # as “英雄们抵达旧路闸门”.  Arrival and intrusion therefore require
        # an explicit threat subject; consequence verbs remain guarded below.
        r"|(?:他们|辉钢的人|财团的人|巡逻队|追兵|车队|援军|增援|"
        r"监察官|财团(?:监察官|巡逻员|机兵|卫兵)|巡逻员|追兵首领|增援队长)"
        r"[^。！？]{0,18}(?:已经|终于|此刻|现在)?(?:到达|冲入|闯入|进入现场)"
        r"|(?:巡逻队|追兵|财团(?:巡逻队|机兵|卫兵)|监察官)"
        r"[^。！？]{0,16}(?:已经|终于|此刻|现在)?(?:进入|抵达)[^。！？]{0,12}"
        r"(?:视野|可见范围|能看见[^。！？]{0,6}(?:位置|范围)|候车厅外缘)"
        # Completion verbs need a threat subject.  Bare “封死出口” often
        # describes the heroes sealing an escape route and must not be confused
        # with the patrol completing its surrounding clock.
        r"|(?:他们|辉钢的人|财团的人|巡逻队|追兵|车队|援军|增援|"
        r"监察官|财团(?:监察官|巡逻员|机兵|卫兵)|巡逻员|追兵首领|增援队长)"
        r"[^。！？]{0,20}(?:已经|终于|此刻|现在|立刻)?(?:全面|临时)?"
        r"(?:包围|封锁|堵死|封死|封住|堵住)"
        r"|(?:驿站|现场|出口|退路|通道|门口)[^。！？]{0,10}(?:已经|终于|此刻|现在)?"
        r"被(?:他们|辉钢的人|财团的人|巡逻队|追兵|援军|增援)?"
        r"(?:全面|临时)?(?:包围|封锁|堵死|封死|封住|堵住)"
        r"|(?:巡逻队|追兵|财团(?:巡逻队|机兵|卫兵))[^。！？]{0,10}"
        r"(?:已经|终于|此刻|现在)?(?:停在|站在|压到|堵到)(?:门外|檐下|门前|现场|出口)"
        r"|(?:(?:巡逻队|追兵|车头)[^。！？]{0,10}(?:撞上|撞向|冲撞|撞击)[^。！？]{0,8}(?:门|闸))"
        r"|(?:撞门|冲撞.{0,4}(?:门|闸)|敲响.{0,4}(?:门|闸)|门板[^。！？]{0,12}(?:受撞|冲力|被顶|闷响))"
        r"|(?:监察官|财团(?:监察官|巡逻员|机兵|卫兵)|巡逻员|追兵首领|增援队长)"
        r"[^。！？]{0,12}(?:已经|终于|此刻|现在)?(?:到了|抵达|赶到|进入(?:现场|驿站|门厅))"
        r"|(?:财团(?:巡逻员|机兵|卫兵)|巡逻员|追兵|增援)[^。！？]{0,12}"
        r"(?:踏上|走上|登上|迈上|跨上|涌上|冲上)(?:石阶|台阶|门阶|门前)"
        r"|(?:巡逻队|追兵|财团(?:巡逻队|机兵|卫兵))[^。！？]{0,18}(?:已经|此刻|现在)?"
        r"(?:在|进入|抵达)[^。！？]{0,10}(?:外侧|外围|门外|入口)[^。！？]{0,12}"
        r"(?:设下|落下|展开|建立)了?(?:临时)?(?:检查线|封锁线|警戒线)"
        r"|(?:巡逻队|追兵|财团(?:巡逻队|机兵|卫兵))[^。！？]{0,18}"
        r"(?:外缘|外侧|外围|门外|入口)[^。！？]{0,8}(?:立停|停下|驻足|设卡|列队)"
        r"|(?:前院外|门外|门前|入口外|驿站外)[，,、\s]{0,4}[^。！？]{0,12}"
        r"(?:巡逻队|追兵|财团(?:巡逻队|机兵|卫兵))[^。！？]{0,8}(?:停步|停下|驻足|设卡|列队)"
        r"|(?:巡逻队|追兵|财团(?:巡逻队|机兵|卫兵))[^。！？]{0,8}(?:停步|停下|驻足)了?"
        r"[^。！？]{0,8}(?:前院外|门外|门前|入口外|驿站外)"
        r"|(?:巡逻队|巡逻者|追兵|财团(?:巡逻队|巡逻者|机兵|卫兵))[^。！？]{0,18}"
        r"(?:散开|展开|列队)[^。！？]{0,14}(?:封住|封锁|堵住)[^。！？]{0,8}"
        r"(?:外沿|外缘|外围|门外|门前|入口|出口|退路)"
        r"|(?:封锁线|包围线|警戒线)[^。！？]{0,12}"
        r"(?:当场|已经|终于|彻底)(?:被)?(?:拉直|合拢|闭合|成形|成型)"
        r"|(?:封锁线|包围线|警戒线)[^。！？]{0,8}"
        r"(?:拉直|合拢|闭合|成形|成型)(?:了|完成)"
    )
    _FLOOD_CLOCK = re.compile(r"潮|水位|淹|没顶|洪水")
    _FLOOD_EVENT = re.compile(r"(?:已经|终于)?(?:淹没|没顶|封死退路|漫过出口|吞没现场)")
    _ALARM_CLOCK = re.compile(r"警报|警戒|暴露|发现")
    _ALARM_EVENT = re.compile(r"(?:警报|警戒)[^。！？]{0,10}(?:全面响起|响彻|已经拉响|升到最高)|全城[^。！？]{0,8}惊动")
    _CHARGE_CLOCK = re.compile(r"蓄力|过载|仪式|施法|咏唱|充能")
    _CHARGE_EVENT = re.compile(r"(?:蓄力|充能|仪式|咏唱)[^。！？]{0,12}(?:已经完成|彻底完成)|(?:法术|魔力)[^。！？]{0,10}(?:释放|爆发)")
    _NEGATION = re.compile(
        r"(?:尚未|还没|没有|并未|未曾|不会|不能|无法|未能|不让|阻止)"
        r"(?:永久|彻底|完全|真正|及时|直接|立即)?\s*$"
        r"|(?:尚未|还没|没有|并未|未曾|不会|不能|无法|不敢)"
        r"(?:说|确认|断定|宣称|表示)[^。！？\n]{0,12}$"
        r"|(?:解除|取消|撤销|停止|暂缓|免于|不再)[^。！？\n]{0,14}$"
    )
    _AFTER_EVENT_NEGATION = re.compile(
        r"^(?:声)?(?:已经|随即|立刻|很快)?(?:停|停止|停下|消失|暂缓|中止|被制止|被阻止)"
    )
    _EVENT_NEGATION = re.compile(
        r"(?:解除|取消|撤销|停止|暂缓|免于|不再|阻止|制止|退出|撤离)"
        r"|(?:不|未|尚未|还没|暂不)[^。！？]{0,5}(?:包围|封锁|堵死|封死|封住|堵住)"
    )
    _PROSPECTIVE_EVENT = re.compile(r"(?:即将|将要|快要|眼看就要|可能|恐怕)")
    _PROSPECTIVE_WARNING = re.compile(
        r"(?:如果|若|再|一旦|继续|只要|等到|待到|否则|不然)"
        r"[^。！？\n]{0,24}(?:就|便|将|会|可能|恐怕|快要|即将|眼看就要)?"
        r"[^。！？\n]{0,8}$"
        r"|(?:将|会|可能|恐怕|快要|即将|眼看就要)[^。！？\n]{0,8}$"
    )

    @classmethod
    def packet(cls, clocks: Iterable[Clock]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for clock in clocks:
            maximum = max(1, int(clock.max_segments or 0))
            current = max(0, min(maximum, int(clock.current or 0)))
            if current >= maximum or str(clock.status or "") in {"resolved", "abandoned", "archived"}:
                continue
            remaining = maximum - current
            ratio = current / maximum
            if ratio < 0.34:
                stage = "只能出现远处征兆；威胁尚未抵达或兑现"
            elif ratio < 0.75:
                stage = "压力可以明显靠近或影响外围；仍不得兑现填满后果"
            else:
                stage = "威胁已到临界边缘；可以描写最后征兆，但填满前不得兑现后果"
            result.append(
                {
                    "name": clock.name,
                    "current": current,
                    "maximum": maximum,
                    "remaining": remaining,
                    "clock_type": clock.clock_type,
                    "stakes": clock.stakes,
                    "completion_consequence": clock.completion_consequence,
                    "authorized_stage": stage,
                }
            )
        return result

    @classmethod
    def violation(cls, text: str, boundaries: Iterable[dict[str, object]]) -> str:
        candidate = str(text or "").strip()
        if not candidate:
            return ""
        for boundary in boundaries:
            current = int(boundary.get("current") or 0)
            maximum = max(1, int(boundary.get("maximum") or 0))
            if current >= maximum:
                continue
            context = " ".join(
                str(boundary.get(key) or "")
                for key in ("name", "stakes", "completion_consequence")
            )
            event_pattern = cls._event_pattern(context)
            if event_pattern is None:
                continue
            for match in event_pattern.finditer(candidate):
                prefix = candidate[max(0, match.start() - 36) : match.start()]
                suffix = candidate[match.end() : match.end() + 20]
                matched_event = match.group(0)
                if (
                    cls._NEGATION.search(prefix)
                    or cls._AFTER_EVENT_NEGATION.search(suffix)
                    or cls._EVENT_NEGATION.search(matched_event)
                ):
                    continue
                # Near-full clocks should feel urgent. Conditional warnings describe
                # what will happen if nobody intervenes; they do not assert that the
                # completion consequence has already occurred.
                if (
                    cls._PROSPECTIVE_WARNING.search(prefix)
                    or cls._PROSPECTIVE_EVENT.search(matched_event)
                ):
                    continue
                return (
                    f"命刻【{boundary.get('name')}】仍为 {current}/{maximum}，"
                    f"候选却提前叙述了填满后才会发生的事件“{match.group(0)}”"
                )
        return ""

    @classmethod
    def _event_pattern(cls, context: str) -> re.Pattern[str] | None:
        if cls._ARRIVAL_CLOCK.search(context):
            return cls._ARRIVAL_EVENT
        if cls._FLOOD_CLOCK.search(context):
            return cls._FLOOD_EVENT
        if cls._ALARM_CLOCK.search(context):
            return cls._ALARM_EVENT
        if cls._CHARGE_CLOCK.search(context):
            return cls._CHARGE_EVENT
        return None
