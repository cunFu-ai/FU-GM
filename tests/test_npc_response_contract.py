from fu_gm.components.npc_response_contract import is_disclosure_promise


def test_continuing_to_tell_known_old_road_content_is_a_disclosure_promise() -> None:
    assert is_disclosure_promise("我会继续谈旧路，并把我知道的旧路相关内容说给你们听。")


def test_giving_a_safe_route_is_a_disclosure_promise() -> None:
    assert is_disclosure_promise("我会当场给出一条让失忆旅人去下一处安全地点的路线。")
