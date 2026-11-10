"""生成售后工单评测集（需求书 §9）。

## 为什么用生成器而不是手写 200 条 JSON

需求书要求的覆盖维度是**组合式**的：10 类问题 × 若干订单事实 × 4 个风险等级 ×
若干异常路径（缺单号/订单不存在/接口超时/越权/重复审批/跨店跨平台）。
手写 JSON 容易出现"某个组合漏了或写重了"，而且改一处要改两百处。
生成器让覆盖矩阵一眼可见，产出的 JSON 仍然提交进仓库供人 review。

## 数据集构成（tickets-1.1.1：228 条，上一版 64 条）

| 分组 | 1.0.0 | 1.1.0 | 期望 |
|---|---|---|---|
| 常规单（10 类问题 × 8 变体，换措辞/换订单事实/换录入角色） | 20 | 80 | issue_type + action + 风险上限（退款类 L3） |
| 退款（含金额差异，金额必须等于实付） | 5 | 12 | action=refund、风险 L3、需主管审批、金额=实付 |
| 风控标记（批量退款/账号限制/疑似欺诈） | 3 | 8 | 风险 L4、转风控 |
| 敏感客户 | 2 | 6 | 风险 L3、需主管审批 |
| 促销口径解释 | 4 | 10 | issue_type、action=reply_only、风险 L1 |
| 破损（需凭证） | 3 | 10 | issue_type、action=reship |
| 物流停滞 ≥48h | 4 | 12 | action=contact_logistics + 政策 v3.0 + 政策补偿金额 |
| 物流停滞 <48h（未达门槛） | 2 | 6 | action=contact_logistics、金额 0 |
| 少件/漏发/错发 | 4 | 12 | action=reship |
| 缺订单号 | 4 | 8 | 拒绝对分析（need_customer_info） |
| 订单不存在 | 3 | 6 | 拒绝对分析 + missing 提到订单号 |
| 接口超时 | 3 | 6 | 拒绝对分析 + missing 提到事实来源暂不可用 |
| 越权（角色 × 越权库） | 3 | 6 | HTTP 403 permission_denied |
| 重复审批幂等 | 2 | 6 | 同一幂等键只产生一次状态迁移 |
| 低置信度（未识别/无凭证） | 2 | 6 | 风险不低于 L2 且明确 missing |
| **跨店/跨平台**（新增维度） | 0 | 12 | 属性带 shop_id/platform，动作与风险**口径不变** |
| **同一句话 × 不同订单事实**（新增维度） | 0 | 12 | 判定跟随事实（停滞小时数/实付金额），不跟客户口述 |
| **二次追问与情绪化措辞**（新增维度） | 0 | 10 | 语气不改变动作与风险口径 |
| **合计** | **64** | **228** | |

三个新维度的选型理由（都不需要新的判定字段）：

1. `cross_shop`：`attributes` 里的 `shop_id` / `platform` 会进入分析查询文本
   （`ticket_analysis._build_query` 会把属性拼进 query），所以它天然检验"多出来的
   属性文本不会污染分类与政策引用"，且政策过滤仍只认 v3.0（不得引用 v4.0 的店铺限定版）；
2. `fact_variant`：同一句话配不同订单事实，判定必须跟随**事实**而不是客户口述
   （用户说"才停了一天"但事实是 48 小时，就该按政策补偿）——这是需求书 §2.2
   "先事实后政策"的直接回归；
3. `escalation_wording`："第二次来问""再不处理就投诉"这类措辞会改变 urgency/情绪，
   但**不得**改变 issue_type / action / 风险口径。优先级字段不在评测判定集内
   （`_evaluate` 没有 priority 检查，本轮不扩判定字段），所以这里只钉住"口径不被语气带跑"。

用法::

    python scripts/generate_ticket_eval_cases.py            # 写入 tests/evaluation/tickets.json
    python scripts/generate_ticket_eval_cases.py --check    # 只校验覆盖矩阵，不写文件
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

OUTPUT = BACKEND / "tests" / "evaluation" / "tickets.json"
DATASET_VERSION = "tickets-1.1.1"

# 演示订单（与适配器夹具一致）：不同订单提供不同事实，用来覆盖"同问题不同事实"的判定差异。
# 前 8 个是 `app/adapters/mock.py` 的命名夹具；`plain_*` / `stalled_*` 是派生订单
# （适配器按订单号哈希派生事实，确定性可复现），用来提供"同一句话、不同事实"的组合。
ORDERS = {
    "signed_not_received": "TB202609241806",   # 已签收、停滞 0h、实付 268.00
    "missing_item": "TB202609241742",          # 已签收、包裹重量偏轻、实付 156.00
    "refundable": "TB202609241501",            # 已签收、实付 89.00（退款用例）
    "damaged": "TB202609240992",               # 已签收、实付 329.00
    "promotion": "TB202609240821",             # 已完成、优惠未生效、实付 189.00
    "stalled": "TB202609240655",               # 已发货、停滞 56h、实付 428.00
    "stalled_48": "TB202609280032",            # 派生：停滞 48h、实付 671.00
    "stalled_48b": "TB202609300051",           # 派生：停滞 48h、实付 536.00
    "stalled_72": "TB202609250004",            # 派生：停滞 72h、实付 347.00
    "stalled_72b": "TB202609270021",           # 派生：停滞 72h、实付 687.00
    "stalled_72c": "TB202609270022",           # 派生：停滞 72h、实付 717.00
    "stalled_72d": "TB202610010062",           # 派生：停滞 72h、实付 402.00
    "stalled_96": "TB202609250008",            # 派生：停滞 96h、实付 58.00
    "stalled_96b": "TB202609250007",           # 派生：停滞 96h、实付 658.00
    "stalled_24": "TB202609250001",            # 派生：停滞 24h（未达门槛）、实付 70.00
    "stalled_24b": "TB202609250006",           # 派生：停滞 24h（未达门槛）、实付 665.00
    "plain_a": "TB202609250002",               # 派生：停滞 0h、实付 484.00
    "plain_b": "TB202609290042",               # 派生：停滞 0h、实付 424.00
    "plain_c": "TB202610010061",               # 派生：停滞 0h、实付 444.00
    "not_found": "TB20260101NODATA",           # 订单不存在（适配器模拟）
    "timeout": "TB20260101TIMEOUT",            # 接口超时（适配器模拟）
}

# 各订单的实付金额（= 退款动作唯一允许引用的金额来源）。
# 期望值写成两位小数字符串：`Recommendation.amount` 落库是 Numeric(12,2)，读回永远是 "xx.xx"。
PAID_AMOUNTS = {
    "signed_not_received": "268.00",
    "missing_item": "156.00",
    "refundable": "89.00",
    "damaged": "329.00",
    "promotion": "189.00",
    "stalled": "428.00",
    "stalled_48": "671.00",
    "stalled_48b": "536.00",
    "stalled_72": "347.00",
    "stalled_72b": "687.00",
    "stalled_72c": "717.00",
    "stalled_72d": "402.00",
    "stalled_96": "58.00",
    "stalled_96b": "658.00",
    "stalled_24": "70.00",
    "stalled_24b": "665.00",
    "plain_a": "484.00",
    "plain_b": "424.00",
    "plain_c": "444.00",
}

# 物流停滞 ≥48 小时时政策写明的单笔补偿上限（v3.0：不超过 ¥30）。
# 金额必须由 `parse_amount` 从政策原文里**提取**，所以这里是"引用"而不是"计算"的断言。
STALLED_COMPENSATION_AMOUNT = "30.00"

# --------------------------------------------------------------------------- 常规：10 类 × 8 变体
# (期望问题类型, 备注标签, ((订单键, 用户原话), ...) × 8)
#
# 变体的三个轴：① 换措辞（同一问题类型的第 2~8 种说法）；② 换订单事实；
# ③ 换渠道优先级语境（同一句话在不同订单上必须得到同一结论）。
NORMAL_CLASSES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "not_shipped",
        "常规-未发货",
        (
            ("signed_not_received", "下单三天了一直没发货，麻烦看看什么情况。"),
            ("plain_a", "已经付款两天了，一直没发货，什么时候能发？"),
            ("damaged", "订单显示未发货，但我已经等了一周了。"),
            ("missing_item", "我买的东西到现在还没有发货。"),
            ("stalled", "下单五天了不发货，是不是忘了？"),
            ("promotion", "请问这单为什么一直没发货？"),
            ("refundable", "付了钱以后一直没有发货，麻烦查一下。"),
            ("plain_b", "还没发货吗？我等了四天了。"),
        ),
    ),
    (
        "logistics_stalled",
        "常规-物流停滞",
        (
            ("stalled_72", "物流已经三天没有更新了，麻烦帮我催一下。"),
            ("plain_a", "快递在转运中心停了很久，一直没动。"),
            ("stalled_48", "包裹物流超时两天了，请帮我跟进一下。"),
            ("stalled_96", "单号显示快递异常，包裹好像丢了。"),
            ("signed_not_received", "物流显示已签收，但我本人没收到。"),
            ("plain_b", "我的快递两天没动了，帮忙催件。"),
            ("stalled_24", "包裹一直不动了，是不是卡在路上了？"),
            ("plain_c", "物流信息一整天没有更新，麻烦看下。"),
        ),
    ),
    (
        "missing_item",
        "常规-少件",
        (
            ("missing_item", "我买了两件，包裹里只收到一件，少了一件。"),
            ("plain_a", "订单里有两样东西，拆开只有一件，少件了。"),
            ("damaged", "收到的包裹内容不全，缺一个配件。"),
            ("plain_b", "只收到一件，另一件不见了，是漏了吗？"),
            ("refundable", "套装只到货一半，缺件。"),
            ("promotion", "包裹里少了一件，套装不全。"),
            ("plain_c", "下单是两件，实际只收到一件，少发了一件。"),
            ("stalled", "三件商品只收到一件，另外两件缺件了。"),
        ),
    ),
    (
        "damaged",
        "常规-破损",
        (
            ("damaged", "收到商品外包装破损，担心影响使用。"),
            ("missing_item", "商品外包装破了一个洞，里面的东西也坏了，需要补发。"),
            ("plain_a", "收到的时候箱子是压坏的，品相很差。"),
            ("plain_b", "杯子碎了，包装里全是碎片。"),
            ("refundable", "拆开发现商品是坏的，用不了。"),
            ("promotion", "外箱瘪了，里面的罐子也压变形了。"),
            ("plain_c", "商品到手就是破损状态，外壳裂了。"),
            ("stalled", "收到的猫窝架子坏了，腿断了一根。"),
        ),
    ),
    (
        "wrong_item",
        "常规-错发",
        (
            ("missing_item", "收到的商品不是我买的那款，发错货了，我要换货。"),
            ("damaged", "发错货了，型号不对。"),
            ("plain_a", "颜色不对，我要的是黑色，收到的是白色。"),
            ("plain_b", "这根本不是我买的商品，快递送错了吧。"),
            ("refundable", "收到的型号不对，跟订单上写的不一样。"),
            ("signed_not_received", "你们发错货了，我要的是大号的。"),
            ("promotion", "订单是猫粮，发来的是猫砂，发错货了。"),
            ("stalled", "商品不是我买的那一款，请核实。"),
        ),
    ),
    (
        "promotion_not_applied",
        "常规-优惠未生效",
        (
            ("promotion", "活动页显示满 199 减 30，我结算时没有减，请帮我看看优惠规则。"),
            ("promotion", "我的券为什么用不了？活动价和优惠能一起用吗？"),
            ("promotion", "这次活动的门槛是多少？我差一点能不能减？"),
            ("plain_a", "满 200 减 50 的优惠没有生效，麻烦帮我看看。"),
            ("plain_b", "下单时领的优惠券在结算页面用不了。"),
            ("refundable", "活动价和页面标注的不一致，活动补贴也没到账。"),
            ("damaged", "请问这次满减活动的规则是什么？我下单时没有减 30。"),
            ("stalled", "我领的优惠券显示已使用，但我根本没下单。"),
        ),
    ),
    (
        "description_mismatch",
        "常规-与描述不符",
        (
            ("damaged", "实物和图片不一样，与描述不符，尺寸也不对。"),
            ("missing_item", "商品跟页面描述不符，材质完全不一样。"),
            ("plain_a", "和图片不一样，颜色差很多。"),
            ("promotion", "货不对板，收到的和宣传的差距很大。"),
            ("refundable", "页面宣传的功能，实际商品没有。"),
            ("signed_not_received", "与描述差异太大，实物很小。"),
            ("plain_b", "商品与描述不符，尺码标注也是错的。"),
            ("plain_c", "和图片不一样，收到的款式不同。"),
        ),
    ),
    (
        "refund",
        "常规-退款",
        (
            ("refundable", "商品不想要了，申请退款，请把钱退给我。"),
            ("signed_not_received", "这个订单我要退款，请尽快处理。"),
            ("damaged", "东西我不要了，直接退款吧。"),
            ("missing_item", "麻烦把这笔钱退给我，我不需要了。"),
            ("promotion", "订单要退款，请帮我办一下。"),
            ("stalled", "这单我申请退款，不想等了。"),
            ("plain_a", "请给我退款，商品用不上了。"),
            ("plain_b", "我要退钱，这个订单不要了。"),
        ),
    ),
    (
        "return",
        "常规-退货",
        (
            ("refundable", "商品不想要了，我要退货，怎么寄回？"),
            ("plain_a", "这个商品我想退货，怎么操作？"),
            ("damaged", "东西用不上，我要退货。"),
            ("missing_item", "麻烦帮我办理退货，我不想留了。"),
            ("promotion", "申请退货，包装还没拆。"),
            ("stalled", "这单我要退回，麻烦给个退货地址。"),
            ("plain_b", "商品不想要了，直接退货吧。"),
            ("signed_not_received", "请帮我走退货流程。"),
        ),
    ),
    (
        "exchange",
        "常规-换货",
        (
            ("damaged", "这个能换一个别的颜色吗？"),
            ("missing_item", "尺码不合适，我想换货。"),
            ("plain_a", "收到的不合适，想换新。"),
            ("refundable", "能换一个同款不同色的吗？"),
            ("plain_b", "我要换货，怎么寄回去？"),
            ("promotion", "这个商品我想换一个款式。"),
            ("stalled", "收到的猫窝不能换货吗？我想换新。"),
            ("plain_c", "想换个尺码，麻烦帮我换货。"),
        ),
    ),
)

# 多角色录入（"换用户身份"这一轴）：同一句话由产品/管理员/审核人建单并触发分析，结论必须一致。
# 依据：`ticket_analysis.start_analysis` 把 `knowledge_bases=["policy_kb"]` 显式写进 filters，
# 检索范围不随发起人角色变化，所以"换了人也换不掉政策依据"是应该被钉住的行为。
NORMAL_ACTOR_ROTATION = {
    ("not_shipped", 8): "pm",
    ("logistics_stalled", 8): "admin",
    ("missing_item", 8): "reviewer",
    ("damaged", 8): "pm",
    ("wrong_item", 8): "admin",
    ("promotion_not_applied", 8): "reviewer",
}

# 每类问题的期望（动作与风险都来自规则，不是主观判断）。
# 风险上限统一写 `max_risk_tier`：需求书只规定"哪些必须审批"，没规定"哪些必须停在低档"，
# 而"低置信度/信息缺失要升档"是明确纪律，用上限断言才不会把升档误判成失败。
NORMAL_EXPECTATIONS: dict[str, dict] = {
    # 这 8 个变体绑定的 mock 订单事实均已发货/签收/完成；客户口述与事实冲突时
    # 必须先核实并升到主管裁定，不能拿“语言变体一致性”掩盖重复补发风险。
    "not_shipped": {
        "issue_type": "not_shipped",
        "action_type": "ask_customer",
        "risk_tier": "L3",
        "approval_requirement": "supervisor",
    },
    "logistics_stalled": {
        "issue_type": "logistics_stalled",
        "action_type": "contact_logistics",
        "max_risk_tier": "L2",
    },
    "missing_item": {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    "damaged": {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    "wrong_item": {"issue_type": "wrong_item", "action_type": "reship", "max_risk_tier": "L2"},
    "promotion_not_applied": {
        "issue_type": "promotion_not_applied",
        "action_type": "reply_only",
        "max_risk_tier": "L1",
    },
    # 与描述不符在规则里没有对应动作分支 → 只能追问客户（不得凭空补发/退款）
    "description_mismatch": {
        "issue_type": "description_mismatch",
        "action_type": "ask_customer",
        "max_risk_tier": "L2",
    },
    # 退款这一类的期望不是"低风险"：按需求书 §3.3，退款一律 L3 且需主管审批。
    # 生成器第一版把它当普通类断言 L2，跑评测时立刻暴露（这正是评测集的作用）。
    "refund": {
        "issue_type": "refund",
        "action_type": "refund",
        "risk_tier": "L3",
        "approval_requirement": "supervisor",
        "requires_approval": True,
    },
    "return": {"issue_type": "return", "action_type": "return_goods", "max_risk_tier": "L2"},
    "exchange": {"issue_type": "exchange", "action_type": "exchange", "max_risk_tier": "L2"},
}

# --------------------------------------------------------------------------- 退款（动作/风险/金额）
# (订单键, 摘要, 用户原话) —— 金额期望统一取 `PAID_AMOUNTS`：退款金额只能引用支付事实。
REFUND_CASES = (
    ("refundable", "申请退款-89元", "商品不想要了，申请退款。"),
    ("signed_not_received", "签收异常要求退款-268元", "包裹显示签收但我没收到，请直接退款给我。"),
    ("damaged", "破损要求退款-329元", "商品破损无法使用，我要退款。"),
    ("missing_item", "少件要求退款-156元", "只收到一件，剩下的那件我要退款。"),
    ("stalled", "停滞要求退款-428元", "物流停滞两天了，我不要了，退款吧。"),
    ("stalled_48", "停滞48h要求退款-671元", "包裹一直不到，我不要了，请退款。"),
    ("plain_a", "下错单要求退款-484元", "商品下错单了，麻烦退钱给我。"),
    ("stalled_72", "等太久要求退款-347元", "等太久了，申请退款，钱退我。"),
    ("plain_b", "不想要了要退款-424元", "我要退款，这个订单不想要了。"),
    ("stalled_96", "催退款-58元", "请把这笔订单的钱退我，谢谢。"),
    ("plain_c", "再次申请退款-444元", "申请退款，麻烦尽快。"),
    ("stalled_24", "未到货要退款-70元", "东西一直没到，我要退款。"),
)

# --------------------------------------------------------------------------- 风控标记（L4）
# (风控标记, 订单键, 摘要, 用户原话)
RISK_FLAG_CASES = (
    (["batch_refund"], "refundable", "批量退款申请", "我要把这批订单全部退款，一共十二单，请一起处理。"),
    (["account_restriction"], "signed_not_received", "账号被限制要求解封", "我的账号被限制了，请帮我解除限制。"),
    (["suspected_fraud"], "damaged", "疑似欺诈投诉", "有人用我的账号下单，我怀疑是被盗刷了，请处理。"),
    (
        ["batch_refund"],
        "plain_a",
        "批量退款申请-第二次",
        "这批 12 单我全部要退款，请一起处理。",
    ),
    (
        ["account_restriction"],
        "missing_item",
        "账号被冻结",
        "我的账号被冻结了，登不上，麻烦解封。",
    ),
    (
        ["suspected_fraud"],
        "stalled_48",
        "盗刷订单投诉",
        "我的账号可能被盗刷了，有两笔陌生订单。",
    ),
    (
        ["batch_refund", "suspected_fraud"],
        "stalled_48b",
        "批量退款+盗刷",
        "同一批订单被连续盗刷退款，怀疑是团伙，这批全部订单都要处理。",
    ),
    (
        ["account_restriction"],
        "plain_b",
        "多个订单被限制",
        "账号被限制了，多个订单都不能下单，请解除限制。",
    ),
)

# --------------------------------------------------------------------------- 敏感客户（L3）
SENSITIVE_CASES = (
    ("refundable", "敏感客户要求退款", "我是老客户了，这次必须给我退款，不然我就去投诉。"),
    ("damaged", "敏感客户要求补发", "已经不是第一次了，我要投诉，请马上给我补发。"),
    (
        "plain_a",
        "敏感客户要求补偿",
        "我是老客户了，这次必须给我补偿，不然我就去曝光。",
    ),
    (
        "missing_item",
        "敏感客户-二次投诉",
        "上次的问题就没解决，这次再不给说法我就投诉到消协。",
    ),
    (
        "stalled_72",
        "敏感客户-停滞投诉",
        "停滞这么久了，我要投诉，请马上给个明确答复。",
    ),
    (
        "damaged",
        "敏感客户-破损二次投诉",
        "商品又破损了，我要投诉，请马上补发。",
    ),
)

# --------------------------------------------------------------------------- 促销口径（L1，只解释）
PROMOTION_CASES = (
    ("promotion", "满减未生效-解释口径", "活动说满 199 减 30，为什么我没有减？"),
    ("promotion", "优惠券不能用", "下单时领的券显示不可用，请问是什么原因？"),
    ("promotion", "活动门槛咨询", "这次活动要满多少才减？我差 20 块能参加吗？"),
    ("promotion", "补贴未到账", "活动补贴说 3 天到账，现在还没到，麻烦看看。"),
    ("plain_a", "满减未生效-换店", "结算页没有减 30，活动是不是有门槛？"),
    ("plain_b", "优惠券不可用-第二次", "优惠券怎么用不了？"),
    ("refundable", "满减活动时效", "满减活动什么时候结束？"),
    ("damaged", "活动价比平时贵", "活动价为什么比平时还贵？"),
    ("stalled", "补贴未到账-催问", "活动补贴 3 天到账，现在还没到，帮我查一下。"),
    ("promotion", "差一点满减", "我差 5 块就能满减，能不能减？"),
)

# --------------------------------------------------------------------------- 破损（需凭证 → 补发）
DAMAGED_CASES = (
    ("damaged", "破损-无凭证", "收到的商品压坏了，怎么办？"),
    ("damaged", "破损-要补发", "外包装破了一个洞，里面的东西也坏了，需要补发。"),
    ("damaged", "破损-已拍照", "商品破损，我已经拍好照片了，请问怎么补发？"),
    ("plain_a", "破损-箱子压坏", "收到的时候箱子是压坏的，里面的东西也变形了。"),
    ("plain_b", "破损-杯子碎了", "玻璃杯到手就碎了，箱子里都是碎片。"),
    ("refundable", "破损-商品是坏的", "收到的商品是坏的，完全用不了。"),
    # 口语化说法（"瘪了/压变形"）：规则原先只认"外包装/破损/压坏/坏了/碎了"，
    # 这三条是评测集暴露出来的关键词缺口，已按最小化补词（见 after_sales.ISSUE_KEYWORDS）。
    ("promotion", "破损-外箱瘪了", "外箱瘪了一块，里面罐子的包装也变形了。"),
    ("plain_c", "破损-外壳裂了", "外壳裂了一道缝，属于运输破损。"),
    ("stalled", "破损-架子腿断了", "猫窝架子的腿断了，属于破损。"),
    ("missing_item", "破损-箱子压瘪", "包装箱被压瘪了一角，商品外壳也有磕碰。"),
)

# --------------------------------------------------------------------------- 物流停滞 ≥48h（政策补偿）
STALLED_CASES = (
    ("stalled", "停滞-48h以上", "物流停滞超过 48 小时，按规定是不是可以补偿？"),
    ("stalled", "停滞-要催件", "包裹两天没动了，麻烦帮我催一下快递。"),
    ("stalled", "停滞-询问补偿标准", "物流停滞了，想了解下补偿标准是多少。"),
    ("stalled", "停滞-超时两天", "快递已经停了 56 小时，我要申请补偿。"),
    ("stalled_48", "停滞-48小时整", "物流已经停滞超过 48 小时，按规定可以补偿吗？"),
    ("stalled_72", "停滞-三天没更新", "包裹三天没有更新了，请帮我催件。"),
    ("stalled_96", "停滞-四天补偿咨询", "快递停滞四天了，补偿标准是多少？"),
    ("stalled_96b", "停滞-申请补偿", "物流信息超过 48 小时没有更新，我要申请补偿。"),
    ("stalled_72b", "停滞-卡在中转站", "包裹卡在中转站三天没动了。"),
    ("stalled_72c", "停滞-两天以上", "物流停滞两天以上，能补偿多少？"),
    ("stalled_72d", "停滞-超时三天", "快递超时三天了，麻烦跟进。"),
    ("stalled_48b", "停滞-按政策补偿", "物流两天没更新，按政策是不是有补偿？"),
)

# --------------------------------------------------------------------------- 停滞未达门槛（只催件）
STALLED_SHORT_CASES = (
    ("signed_not_received", "停滞-12小时", "包裹好像有点慢，才停了一天不到。"),
    ("missing_item", "停滞-未达门槛", "物流一天没更新了，要不要补偿？"),
    ("stalled_24", "停滞-24小时", "物流才停了一天，需要补偿吗？"),
    ("stalled_24b", "停滞-一天没动", "包裹一天没动了，麻烦帮我看看。"),
    ("plain_a", "停滞-刚停一天", "快递刚停了一天，还没到 48 小时。"),
    ("plain_b", "停滞-今天没更新", "物流今天没有更新，是不是正常？"),
)

# --------------------------------------------------------------------------- 少件/漏发/错发（补发）
RESHIP_CASES = (
    ("missing_item", "少件-补发", "只收到一件，请补发缺的那件。"),
    ("signed_not_received", "漏发-补发", "订单里有两样东西，包裹里只有一样，漏发了。"),
    ("missing_item", "错发-换货", "收到的型号不对，请给我换货。"),
    ("damaged", "少件-包裹轻", "包裹明显比订单标注的轻，应该是少件了。"),
    ("plain_a", "少件-补发一件", "包裹里少了一件，请补发。"),
    ("plain_b", "漏发-赠品", "收到的商品不全，漏发了赠品。"),
    ("refundable", "错发-型号错", "发错型号了，麻烦给我寄一件正确的。"),
    ("promotion", "少件-数量不对", "收到的数量不对，比订单少了一件。"),
    ("damaged", "少件-只到一件", "订单里两件只收到一件，请补发剩下的。"),
    ("signed_not_received", "漏发-缺一件", "拆开只有一件，另一个没有，漏发了。"),
    ("stalled", "少件-缺配件", "包裹内容不全，缺少配件。"),
    ("plain_c", "错发-颜色错", "发错颜色了，请补发正确的那款。"),
)

# --------------------------------------------------------------------------- 缺订单号（拒绝对分析）
MISSING_ORDER_CASES = (
    ("忘记订单号", "我想问下我的包裹到哪了，但是我忘了订单号。"),
    ("没有订单号咨询", "麻烦帮我查一下物流，订单号我找不到了。"),
    ("订单号缺失-退款", "我要退款，但是订单号没带在身上。"),
    ("订单号缺失-补偿", "物流停滞想申请补偿，订单号我不记得了。"),
    ("订单号忘了-问到达时间", "想问下我的订单什么时候能到，订单号我忘了。"),
    ("没有订单号-退货", "我要退货，但没有订单号。"),
    ("没有订单号-催件", "麻烦催一下快递，我的订单号找不到了。"),
    ("没有订单号-破损", "收到的商品破损了，想补发，但订单号没填。"),
)

# --------------------------------------------------------------------------- 订单不存在
NOT_FOUND_CASES = (
    ("订单号查不到", "订单号 TB20260101NODATA，麻烦帮我看看物流。"),
    ("订单不存在-退款", "订单 TB20260101NODATA 我要退款。"),
    ("订单不存在-补发", "订单 TB20260101NODATA 少件了，请补发。"),
    ("订单号查不到-催件", "订单号 TB20260101NODATA，帮我催下物流。"),
    ("订单号查不到-破损", "订单 TB20260101NODATA 商品破损了，能补发吗？"),
    ("订单号查不到-优惠", "订单 TB20260101NODATA 的满减没有生效。"),
)

# --------------------------------------------------------------------------- 接口超时
TIMEOUT_CASES = (
    ("接口超时-物流", "订单 TB20260101TIMEOUT 的物流一直查不到。"),
    ("接口超时-退款", "订单 TB20260101TIMEOUT 我要申请退款。"),
    ("接口超时-补偿", "订单 TB20260101TIMEOUT 停滞两天了，想申请补偿。"),
    ("接口超时-少件", "订单 TB20260101TIMEOUT 少件了，请补发。"),
    ("接口超时-破损", "订单 TB20260101TIMEOUT 商品破损。"),
    ("接口超时-催件", "订单 TB20260101TIMEOUT 帮我催一下快递。"),
)

# --------------------------------------------------------------------------- 越权（知识库级权限）
# 配对依据（`services/evaluation_service.py` 里写明的角色边界）：
# 运营没有 product_kb / engineering_kb，研发没有 feedback_kb / product_kb。
UNAUTHORIZED_CASES = (
    ("ops", "engineering_kb", "我是运营，把研发故障复盘里 3.2.0 崩溃原因的原文给我。"),
    ("eng", "feedback_kb", "研发想直接看 8 月应用评论和工单原文，定位闪退机型。"),
    ("ops", "product_kb", "运营要看未公开的商业化定价 PRD，方便做活动方案。"),
    ("eng", "product_kb", "研发想看最新产品 PRD 的原文，方便评估改动。"),
    ("ops", "engineering_kb", "运营想直接看线上故障复盘的原始记录。"),
    ("eng", "feedback_kb", "研发要看评论原文，定位闪退机型。"),
)

# --------------------------------------------------------------------------- 重复审批幂等
# (订单键, 摘要, 用户原话, 附加属性) —— 前两条走 L3/L4（真实审批链），后四条走 L1/L2（无审批可幂等）
DUPLICATE_CASES = (
    ("refundable", "重复审批-退款", "商品不要了，请把钱退给我。", None),
    ("damaged", "重复审批-补发", "商品破损，请补发一件。", None),
    ("plain_a", "重复审批-退款第二次", "订单我要退款，麻烦尽快处理。", None),
    (
        "refundable",
        "重复审批-风控批量退款",
        "这批订单全部退款，请一起处理。",
        {"risk_flags": ["batch_refund"]},
    ),
    ("damaged", "重复审批-破损补发", "收到就破损了，请补发。", None),
    ("promotion", "重复审批-优惠解释", "满减没有生效，帮我看看。", None),
)

# --------------------------------------------------------------------------- 低置信度
# 措辞里故意不含任何分类关键词：规则应给出"未识别"并索取信息，而不是猜一个类型。
LOW_CONFIDENCE_CASES = (
    ("signed_not_received", "低置信度-信息不足", "有点问题，你帮我看看。"),
    ("missing_item", "低置信度-无凭证", "东西好像不对，具体我说不上来。"),
    ("plain_a", "低置信度-描述含糊", "东西用起来怪怪的，我也说不上来哪里有问题。"),
    ("plain_b", "低置信度-只说要看看", "麻烦你们看一下，我这边好像有点问题。"),
    ("plain_c", "低置信度-未说明诉求", "有个情况想反馈一下，你帮我看看怎么处理。"),
    ("damaged", "低置信度-只发截图", "我发了个截图，你看下这是什么情况。"),
)

# --------------------------------------------------------------------------- 新维度 1：跨店/跨平台
# (订单键, 摘要, 用户原话, 属性, 期望)
# 属性只进工单 attributes（会拼进分析查询文本），不改变过滤口径：动作与风险必须照常判。
CROSS_SHOP_CASES = (
    (
        "missing_item",
        "跨店-少件（抖音店）",
        "在你们抖音店买的，包裹里只收到一件。",
        {"shop_id": "S02", "platform": "抖音商城"},
        {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "damaged",
        "跨店-破损（天猫店）",
        "天猫店发的货，外包装破损了。",
        {"shop_id": "S01", "platform": "天猫"},
        {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "stalled_48",
        "跨店-停滞（京东店）",
        "京东店的订单物流停滞超过两天了。",
        {"shop_id": "S03", "platform": "京东"},
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "max_risk_tier": "L2",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "promotion",
        "跨平台-优惠未生效（拼多多）",
        "拼多多店的满减没有生效。",
        {"shop_id": "S04", "platform": "拼多多"},
        {"issue_type": "promotion_not_applied", "action_type": "reply_only", "max_risk_tier": "L1"},
    ),
    (
        "plain_a",
        "跨店-错发（手淘旗舰店）",
        "手淘旗舰店发错货了，型号不对。",
        {"shop_id": "S05", "platform": "手淘"},
        {"issue_type": "wrong_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "stalled_72",
        "跨店-催件（抖音店）",
        "抖音店的快递三天没动了，帮我催一下。",
        {"shop_id": "S02", "platform": "抖音商城"},
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "max_risk_tier": "L2",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "plain_b",
        "跨店-退货（天猫旗舰店）",
        "天猫旗舰店买的，我想退货。",
        {"shop_id": "S01", "platform": "天猫"},
        {"issue_type": "return", "action_type": "return_goods", "max_risk_tier": "L2"},
    ),
    (
        "refundable",
        "跨店-退款（京东自营）",
        "京东自营买的，申请退款。",
        {"shop_id": "S03", "platform": "京东"},
        {
            "issue_type": "refund",
            "action_type": "refund",
            "risk_tier": "L3",
            "approval_requirement": "supervisor",
            "requires_approval": True,
            "amount": "89.00",
        },
    ),
    (
        "plain_c",
        "跨店-少件（抖音店-第二次）",
        "抖音店下单两件只收到一件。",
        {"shop_id": "S02", "platform": "抖音商城"},
        {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "stalled_96",
        "跨平台-停滞补偿（拼多多）",
        "拼多多店的包裹停滞四天了，按政策有补偿吗？",
        {"shop_id": "S04", "platform": "拼多多"},
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "max_risk_tier": "L2",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "damaged",
        "跨店-破损已拍照（抖音店）",
        "抖音店买的猫爬架外包装破损，已拍照。",
        {"shop_id": "S02", "platform": "抖音商城"},
        {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "promotion",
        "跨店-活动门槛（天猫店）",
        "天猫店这次活动的门槛是多少？",
        {"shop_id": "S01", "platform": "天猫"},
        {"issue_type": "promotion_not_applied", "action_type": "reply_only", "max_risk_tier": "L1"},
    ),
)

# --------------------------------------------------------------------------- 新维度 2：同一句话 × 不同订单事实
# 每两条例子的用户原话完全一致，只有订单事实不同 —— 判定必须跟着事实走：
# 停滞小时数决定要不要按政策补偿，实付金额决定退款金额，签收状态决定是不是"签收异常"。
FACT_VARIANT_CASES = (
    (
        "事实差异-停滞补偿申请·事实 72h",
        "stalled_72",
        "物流停滞两天以上，能补偿吗？",
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "max_risk_tier": "L2",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "事实差异-停滞补偿申请·事实 0h",
        "plain_a",
        "物流停滞两天以上，能补偿吗？",
        {"issue_type": "logistics_stalled", "action_type": "contact_logistics", "amount": "0"},
    ),
    (
        "事实差异-少件补发·事实含重量异常",
        "missing_item",
        "包裹里少了一件，请补发。",
        {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "事实差异-少件补发·事实无重量",
        "plain_b",
        "包裹里少了一件，请补发。",
        {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "事实差异-退款金额·实付 189",
        "promotion",
        "这个订单我要退款。",
        {
            "action_type": "refund",
            "risk_tier": "L3",
            "approval_requirement": "supervisor",
            "requires_approval": True,
            "amount": "189.00",
        },
    ),
    (
        "事实差异-退款金额·实付 658",
        "stalled_96b",
        "这个订单我要退款。",
        {
            "action_type": "refund",
            "risk_tier": "L3",
            "approval_requirement": "supervisor",
            "requires_approval": True,
            "amount": "658.00",
        },
    ),
    # 客户口述"才停了一天"，但事实是 48 小时：判定必须按事实，不能按口述
    (
        "事实差异-口述 1 天·事实 48h",
        "stalled_48",
        "快递好像才停了一天，能补偿吗？",
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "事实差异-口述 1 天·事实 24h",
        "stalled_24",
        "快递好像才停了一天，能补偿吗？",
        {"issue_type": "logistics_stalled", "action_type": "contact_logistics", "amount": "0"},
    ),
    (
        "事实差异-签收异常·事实已签收",
        "signed_not_received",
        "包裹显示签收但我没收到，怎么办？",
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "amount": "0",
        },
    ),
    (
        "事实差异-签收异常·事实运输中 72h",
        "stalled_72c",
        "包裹显示签收但我没收到，怎么办？",
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "事实差异-破损补发·事实 329 元订单",
        "damaged",
        "商品破损了，请补发。",
        {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "事实差异-破损补发·事实 444 元订单",
        "plain_c",
        "商品破损了，请补发。",
        {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    ),
)

# --------------------------------------------------------------------------- 新维度 3：二次追问与情绪化措辞
# 措辞（催办次数、情绪、投诉威胁）只应影响 urgency/情绪，**不得**改变分类、动作与风险口径。
ESCALATION_CASES = (
    (
        "二次追问-少件",
        "missing_item",
        "这是我第二次来问了，包裹里少了一件，上次没人回复。",
        {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "二次追问-破损",
        "damaged",
        "第二次反馈了，商品外包装破损，一直没人处理。",
        {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "三次催办-停滞",
        "stalled_72d",
        "已经第三次来催了，物流停滞三天还没动。",
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "max_risk_tier": "L2",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "二次追问-优惠",
        "promotion",
        "又来问了，满减还是没有生效。",
        {"issue_type": "promotion_not_applied", "action_type": "reply_only", "max_risk_tier": "L1"},
    ),
    (
        "催办-退货",
        "plain_a",
        "麻烦尽快处理，我要退货，已经等三天了。",
        {"issue_type": "return", "action_type": "return_goods", "max_risk_tier": "L2"},
    ),
    (
        "二次追问-退款",
        "plain_b",
        "再次申请退款，麻烦尽快处理。",
        {
            "action_type": "refund",
            "risk_tier": "L3",
            "approval_requirement": "supervisor",
            "requires_approval": True,
            "amount": "424.00",
        },
    ),
    (
        "情绪升级-停滞",
        "stalled_48b",
        "太差了！物流停滞两天了，再不处理我就投诉。",
        {
            "issue_type": "logistics_stalled",
            "action_type": "contact_logistics",
            "max_risk_tier": "L2",
            "policy_version_included": "v3.0",
            "amount": STALLED_COMPENSATION_AMOUNT,
        },
    ),
    (
        "再次反馈-少件",
        "plain_c",
        "又要麻烦你们了，包裹里缺件，请补发。",
        {"issue_type": "missing_item", "action_type": "reship", "max_risk_tier": "L2"},
    ),
    (
        "再次追问-优惠券",
        "promotion",
        "第二次问了，优惠券还是用不了。",
        {"issue_type": "promotion_not_applied", "action_type": "reply_only", "max_risk_tier": "L1"},
    ),
    (
        "再次反馈-破损",
        "damaged",
        "上次的照片已经发过了，商品还是破损状态，麻烦补发。",
        {"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
    ),
)


def build_cases() -> list[dict]:
    cases: list[dict] = []

    def add(
        index: int,
        group: str,
        subject: str,
        content: str,
        *,
        order_key: str | None = None,
        expect: dict | None = None,
        user: str = "ops",
        attributes: dict | None = None,
        request_kb: str | None = None,
        notes: str = "",
    ) -> None:
        case: dict = {
            "case_id": f"tk-eval-{index:03d}",
            "group": group,
            "subject": subject,
            "content": content,
            "user": user,
            "order_id": ORDERS.get(order_key) if order_key else None,
            "customer_id": f"U{1000 + index}",
            "expect": expect or {},
            "notes": notes,
        }
        if attributes:
            case["attributes"] = attributes
        if request_kb:
            case["request_knowledge_bases"] = [request_kb]
        cases.append(case)

    index = 0

    # ---- 常规：10 类问题 × 8 变体（换措辞 + 换订单事实 + 换录入角色，检验"事实参与判定"）----
    for issue_type, label, variants in NORMAL_CLASSES:
        for offset, (order_key, content) in enumerate(variants, start=1):
            index += 1
            expectation = dict(NORMAL_EXPECTATIONS[issue_type])
            if issue_type == "refund":
                # 退款金额只能引用支付事实：期望值就是该订单的实付金额
                expectation["amount"] = PAID_AMOUNTS[order_key]
            actor = NORMAL_ACTOR_ROTATION.get((issue_type, offset), "ops")
            add(
                index,
                "normal",
                f"{label}-{offset}",
                content,
                order_key=order_key,
                expect=expectation,
                user=actor,
                notes=(
                    "客户称未发货，但 mock 订单事实已进入履约；必须先核实冲突，不得重复补发"
                    if issue_type == "not_shipped"
                    else f"期望归类为 {issue_type}；同一问题换措辞/换订单事实/换录入角色（{actor}），结论不变"
                ),
            )

    # ---- 退款：动作必须是 refund、风险 L3、需主管审批、金额=实付 ----
    for order_key, subject, content in REFUND_CASES:
        index += 1
        if order_key == "signed_not_received":
            # “已签收但客户说没收到”是事实冲突，必须先核实；直接退款会重复赔付。
            expect = {
                "issue_type": "logistics_stalled",
                "action_type": "ask_customer",
                "risk_tier": "L3",
                "approval_requirement": "supervisor",
                "requires_approval": True,
            }
            notes = "签收与未收到冲突：先核实签收凭证，不得直接退款"
        else:
            expect = {
                "action_type": "refund",
                "risk_tier": "L3",
                "approval_requirement": "supervisor",
                "requires_approval": True,
                "amount": PAID_AMOUNTS[order_key],
            }
            notes = "退款一律 L3：金额引用实付金额，需主管审批"
        add(
            index,
            "refund",
            subject,
            content,
            order_key=order_key,
            expect=expect,
            notes=notes,
        )

    # ---- 风控标记：L4，转风控且禁止自动执行 ----
    for flags, order_key, subject, content in RISK_FLAG_CASES:
        index += 1
        add(
            index,
            "risk_flag",
            subject,
            content,
            order_key=order_key,
            attributes={"risk_flags": flags},
            expect={"risk_tier": "L4", "approval_requirement": "risk_control", "requires_approval": True},
            notes=f"风控标记 {flags}：必须转风控",
        )

    # ---- 敏感客户：L3 ----
    for order_key, subject, content in SENSITIVE_CASES:
        index += 1
        add(
            index,
            "sensitive",
            subject,
            content,
            order_key=order_key,
            attributes={"sensitive_customer": True},
            expect={"risk_tier": "L3", "approval_requirement": "supervisor", "requires_approval": True},
            notes="敏感客户（投诉倾向）：需主管审批",
        )

    # ---- 促销口径解释：L1，动作 reply_only ----
    for order_key, subject, content in PROMOTION_CASES:
        index += 1
        add(
            index,
            "promotion",
            subject,
            content,
            order_key=order_key,
            expect={"issue_type": "promotion_not_applied", "action_type": "reply_only", "max_risk_tier": "L1"},
            notes="优惠类先解释口径，不涉及赔付",
        )

    # ---- 破损：需凭证 ----
    for order_key, subject, content in DAMAGED_CASES:
        index += 1
        add(
            index,
            "damaged",
            subject,
            content,
            order_key=order_key,
            expect={"issue_type": "damaged", "action_type": "reship", "max_risk_tier": "L2"},
            notes="破损优先补发；凭证缺失要写进 missing_information",
        )

    # ---- 物流停滞 ≥48h：政策补偿金额（金额只能从政策原文提取） ----
    for order_key, subject, content in STALLED_CASES:
        index += 1
        add(
            index,
            "stalled",
            subject,
            content,
            order_key=order_key,
            expect={
                "issue_type": "logistics_stalled",
                "action_type": "contact_logistics",
                "policy_version_included": "v3.0",
                "amount": STALLED_COMPENSATION_AMOUNT,
            },
            notes="停滞超 48 小时：引用 v3.0 政策，补偿金额取自政策条款（不计算）",
        )

    # ---- 停滞未达门槛 ----
    for order_key, subject, content in STALLED_SHORT_CASES:
        index += 1
        add(
            index,
            "stalled_short",
            subject,
            content,
            order_key=order_key,
            expect={"action_type": "contact_logistics", "amount": "0"},
            notes="未达 48 小时门槛：只催件不补偿（既不看客户口述，也不给钱）",
        )

    # ---- 少件/漏发/错发：补发 ----
    for order_key, subject, content in RESHIP_CASES:
        index += 1
        add(
            index,
            "reship",
            subject,
            content,
            order_key=order_key,
            expect={"action_type": "reship", "max_risk_tier": "L2"},
            notes="补发不涉及赔付金额",
        )

    # ---- 缺订单号：拒绝对分析 ----
    for subject, content in MISSING_ORDER_CASES:
        index += 1
        add(
            index,
            "missing_order",
            subject,
            content,
            expect={"analyze_status": 409, "ticket_status": "need_customer_info", "missing": ["订单号"]},
            notes="没有订单号 → 不分析，等客户补充",
        )

    # ---- 订单不存在 ----
    for subject, content in NOT_FOUND_CASES:
        index += 1
        add(
            index,
            "order_not_found",
            subject,
            content,
            order_key="not_found",
            expect={"analyze_status": 409, "ticket_status": "need_customer_info", "missing_contains": "订单号"},
            notes="订单号查不到 → 事实查询失败 → 进人工/等客户补充",
        )

    # ---- 接口超时 ----
    for subject, content in TIMEOUT_CASES:
        index += 1
        add(
            index,
            "adapter_timeout",
            subject,
            content,
            order_key="timeout",
            # 2026-11-04 起事实源故障统一提示"事实来源暂不可用"，不再伪装成客户资料缺失；
            # 断言跟着真实契约走，避免把已改进的提示误判成回归。
            expect={
                "analyze_status": 409,
                "ticket_status": "need_customer_info",
                "missing_contains": "事实来源暂不可用",
            },
            notes="接口超时必须落成结构化事实并进人工，不能让工单 500",
        )

    # ---- 越权：必须 403 ----
    for user, kb, content in UNAUTHORIZED_CASES:
        index += 1
        add(
            index,
            "unauthorized",
            "越权访问知识库",
            content,
            user=user,
            request_kb=kb,
            expect={"analyze_status": 403, "error_code": "permission_denied"},
            notes=f"{user} 访问 {kb}：必须被拒绝（知识库级权限）",
        )

    # ---- 重复审批幂等 ----
    for order_key, subject, content, attributes in DUPLICATE_CASES:
        index += 1
        add(
            index,
            "duplicate_decision",
            subject,
            content,
            order_key=order_key,
            attributes=attributes,
            expect={"idempotent_decision": True},
            notes="同一幂等键重复审批：只允许一次状态迁移",
        )

    # ---- 低置信度 ----
    for order_key, subject, content in LOW_CONFIDENCE_CASES:
        index += 1
        add(
            index,
            "low_confidence",
            subject,
            content,
            order_key=order_key,
            expect={"min_risk_tier": "L2"},
            notes="信息不足时不得停在「可自动」档位；要给出 missing_information",
        )

    # ---- 新维度 1：跨店/跨平台（属性不影响判定口径） ----
    for order_key, subject, content, attributes, expectation in CROSS_SHOP_CASES:
        index += 1
        add(
            index,
            "cross_shop",
            subject,
            content,
            order_key=order_key,
            attributes=attributes,
            expect=expectation,
            notes="跨店/跨平台属性进入查询文本，但动作与风险口径不变；政策仍只引用 v3.0",
        )

    # ---- 新维度 2：同一句话 × 不同订单事实 ----
    for subject, order_key, content, expectation in FACT_VARIANT_CASES:
        index += 1
        add(
            index,
            "fact_variant",
            subject,
            content,
            order_key=order_key,
            expect=expectation,
            notes="同一句客户原话：判定跟随订单事实（停滞小时数/实付金额/签收状态），不跟随口述",
        )

    # ---- 新维度 3：二次追问与情绪化措辞 ----
    for subject, order_key, content, expectation in ESCALATION_CASES:
        index += 1
        add(
            index,
            "escalation_wording",
            subject,
            content,
            order_key=order_key,
            expect=expectation,
            notes="催办次数与情绪只影响 urgency/情绪，不得改变分类、动作与风险口径",
        )

    return cases


def coverage(cases: list[dict]) -> dict:
    groups: dict[str, int] = {}
    for case in cases:
        groups[case["group"]] = groups.get(case["group"], 0) + 1
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description="生成售后工单评测集")
    parser.add_argument("--check", action="store_true", help="只打印覆盖矩阵，不写文件")
    args = parser.parse_args()

    cases = build_cases()
    groups = coverage(cases)
    print(f"数据集版本 {DATASET_VERSION}，用例数 {len(cases)}")
    for group, count in sorted(groups.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {group:<20} {count:>3}")

    if args.check:
        return 0
    payload = {"dataset_version": DATASET_VERSION, "cases": cases}
    # newline="\n"：仓库用 `.gitattributes` 钉死 LF。默认的文本模式写入会把 \n 翻成 CRLF，
    # 于是每次重新生成都产生"整个文件都变了"的伪 diff（本地 Windows + CI Linux 的经典坑）。
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"已写入 {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
