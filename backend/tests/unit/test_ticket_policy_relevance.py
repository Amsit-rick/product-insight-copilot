from app.services.ticket_policy_relevance import filter_policy_evidence


def test_promotion_policy_filter_excludes_logistics_compensation_rules():
    evidence = [
        {
            "title": "售后政策-物流时效与补偿标准v3.0.md",
            "snippet": "物流停滞超过48小时可申请补偿，补偿以优惠券或余额形式发放。",
        },
        {"title": "店铺满减活动说明", "snippet": "特价商品不计入满减门槛。"},
    ]

    filtered = filter_policy_evidence("promotion_not_applied", evidence)

    assert filtered == [evidence[1]]


def test_policy_filter_fails_closed_for_unknown_issue_type():
    evidence = [{"title": "物流时效标准", "snippet": "停滞超过48小时可申请补偿。"}]

    assert filter_policy_evidence("other", evidence) == []


def test_policy_filter_accepts_issue_specific_content_when_title_is_generic():
    evidence = [{"title": "售后处理规范", "section_title": "优惠活动适用范围", "content": "满减活动门槛。"}]

    assert filter_policy_evidence("promotion_not_applied", evidence) == evidence


def test_refund_policy_filter_excludes_compensation_clause_that_only_mentions_refunds():
    evidence = [
        {
            "title": "售后政策-物流时效与补偿标准v3.0.md",
            "section_title": "2. 补偿标准",
            "content": "补偿以优惠券或余额形式发放，不直接退款；已产生实际损失的按第 4 条走退款流程。",
        }
    ]

    assert filter_policy_evidence("refund", evidence) == []


def test_refund_policy_filter_accepts_actual_refund_clause():
    evidence = [
        {
            "title": "售后处理规范",
            "section_title": "4. 退款与审批",
            "content": "退款金额以支付事实的实付金额为准，一律需要主管审批。",
        }
    ]

    assert filter_policy_evidence("refund", evidence) == evidence
