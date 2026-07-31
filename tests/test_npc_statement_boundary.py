from fu_gm.components.npc_statement_boundary import NPCStatementBoundary


def test_boundary_rejects_distinctive_statement_transferred_to_another_npc() -> None:
    metadata = {
        "npc_speakers": [
            {
                "npc": "灰金短斗篷使者",
                "public_statement": "条件我已经说清了，要谈，就别让他再站回原来的入口。",
            }
        ]
    }
    ledger = [
        {
            "npc": "失名旅人",
            "statements": [
                "真正能顶住门外使者的，只是路还在、但人不能再站在原来的入口上这一句。"
            ],
        },
        {
            "npc": "灰金短斗篷使者",
            "statements": ["今天只认一项：你们知道的那条去路。"],
        },
    ]

    violation = NPCStatementBoundary.violation(metadata, ledger)

    assert "台词归属冲突" in violation
    assert "失名旅人" in violation


def test_boundary_allows_npc_to_continue_its_own_public_position() -> None:
    metadata = {
        "npc_speakers": [
            {
                "npc": "灰金短斗篷使者",
                "public_statement": "今天只认你们知道的那条去路，不换别的。",
            }
        ]
    }
    ledger = [
        {
            "npc": "失名旅人",
            "statements": ["人不能再站在原来的入口上。"],
        },
        {
            "npc": "灰金短斗篷使者",
            "statements": ["今天只认一项：你们知道的那条去路。"],
        },
    ]

    assert NPCStatementBoundary.violation(metadata, ledger) == ""
