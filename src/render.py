"""报表渲染:集团财务版、城市经营版、新旧周报并排对比。

披露纪律:
- 集团版只供财务:含待分运输池、行程级运费明细、全公司合计。
- 经营版按城投递:只有本城可披露汇总与本城待办;不含全公司合计、不含他城数字、
  不含行程总价与待分池金额(跨城未分费用只给财务汇总),避免由运费调账明细反推他城订单。
- 各城不得拿自己那一列手工拼回全公司数:经营版干脆不提供可拼的列。
"""
from __future__ import annotations

from fractions import Fraction

from .bridge import Bridge, WeekParts
from .findings import Item, RecapDisposition
from .ledger import Ledger
from .model import CHANNELS, SKUS


def yuan(cents: int | Fraction, sign: bool = False) -> str:
    """分 → 元,两位小数,千分位。"""
    if isinstance(cents, Fraction):
        cents = _round_half_up(cents)
    a = abs(cents)
    out = f"{a // 100:,}.{a % 100:02d}"
    if cents < 0:
        return "-" + out
    return ("+" + out) if sign and cents > 0 else out


def _round_half_up(x: Fraction) -> int:
    return int(x + Fraction(1, 2)) if x >= 0 else -int(-x + Fraction(1, 2))


def syuan(cents: int | Fraction) -> str:
    return yuan(cents, sign=True)


CALIBER = """\
口径:客户成交额=总标价−券;券是自担活动费,外部补贴是可确认收入,两者不同性质。\
归属取承诺店(不按实际核销门店),门店须在生效期内。规格按 sku,同品名不同规格不并名。\
运费一次行程只支出一次,按业务登记的计费重量分摊;行程总价已知而部分重量未登记时,\
待分部分独列「待分运输成本」,无资料行的运费是「待定」而不是 0。\
退款冲减原销售观察周,现金流出单列在实际现金周。"""


def header(title: str, cut: str, extra: str = "") -> str:
    return f"# {title}\n\n资料切点:{cut}(北京时间,仅含此时刻前已知的记录)\n\n{CALIBER}\n{extra}\n"


def checks_section(ledger: Ledger) -> str:
    lines = ["## 对账校验(任一不过不得发报)", "", "| 校验 | 结果 | 说明 |", "| --- | --- | --- |"]
    for name, ok, note in ledger.checks:
        lines.append(f"| {name} | {'通过' if ok else '**不通过**'} | {note} |")
    return "\n".join(lines) + "\n"


def week_table(ledger: Ledger, weeks: tuple[str, str], cities: list[str],
               group: bool) -> str:
    """周毛利总账。group=True 时带待分池与集团列(仅财务);待分池归在行程所在周。"""
    trip_week = {l.trip: l.week for l in ledger.lines}
    pool_by_week = {w: sum(p.cents for p in ledger.pools if trip_week.get(p.trip) == w)
                    for w in weeks}
    cols = cities + (["集团"] if group else [])
    out = ["## 周毛利总账(销售观察周口径,单位:元)", ""]
    for week in weeks:
        first, last = ledger_week_span(ledger, week)
        out.append(f"### {week}({first}~{last})")
        out.append("")
        out.append("| 组件 | " + " | ".join(cols) + " |")
        out.append("| --- |" + " --- |" * len(cols))
        parts = {c: _parts(ledger, week, c) for c in cities}
        if group:
            parts["集团"] = _parts(ledger, week, None)
        rows = [
            ("客户成交额", lambda p: p.net_sales),
            ("外部补贴(收入)", lambda p: p.subsidy),
            ("商品成本", lambda p: -p.product_cost),
            ("已分运输成本", lambda p: -p.freight_alloc),
            ("退款净冲减", lambda p: -p.refund_net),
            ("**毛利(已分口径)**", lambda p: p.margin),
        ]
        for label, get in rows:
            out.append(f"| {label} | " + " | ".join(yuan(get(parts[c])) for c in cols) + " |")
        out.append("| 其中:自担券(活动费) | " + " | ".join(yuan(parts[c].coupon) for c in cols) + " |")
        out.append("| 运费待定行数 | " + " | ".join(str(parts[c].freight_pending_lines) for c in cols) + " |")
        if group:
            pool = pool_by_week.get(week, 0)
            gm = parts["集团"].margin
            out.append(f"| 待分运输成本(集团独列) | " + " | ".join(["—"] * len(cities) + [yuan(-pool)]) + " |")
            out.append(f"| **集团毛利(含待分)** | " + " | ".join(["—"] * len(cities) + [yuan(gm - pool)]) + " |")
        out.append("")
    return "\n".join(out) + "\n"


def ledger_week_span(ledger: Ledger, week: str) -> tuple[str, str]:
    days = sorted(l.sold_day for l in ledger.lines if l.week == week)
    return (days[0], days[-1]) if days else ("—", "—")


def _parts(ledger: Ledger, week: str, city: str | None) -> WeekParts:
    from .bridge import week_parts
    return week_parts(ledger.lines_of(week=week, city=city))


def bridge_table(bridges: dict[str, Bridge], pool_delta: int | None,
                 heading: bool = True) -> str:
    """差异桥。城市行合计=集团行;结构为其中项,标注不可跨片相加。"""
    out = []
    if heading:
        out += ["## 周毛利差异桥(W2 对 W1,单位:元)", ""]
    out += ["因子合计恰好等于毛利差;「结构」是销量的其中项,只在本切片口径内有效,**不可跨切片相加**。",
            ""]
    head = ["切片", "销量", "其中:纯量", "其中:结构", "售价", "活动费(自担券)", "外部补贴",
            "商品成本", "交互", "结构进出", "运输变化", "退款变化", "合计"]
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + " --- |" * len(head))
    rounded: dict[str, dict[str, int]] = {}
    for label, br in bridges.items():
        if label == "集团":
            continue
        rounded[label] = _bridge_row_rounded(br)
    comp_keys = ["volume", "price", "activity", "subsidy", "cost", "interaction",
                 "structural", "freight", "refund", "total"]
    group = {k: sum(rounded[c][k] for c in rounded) for k in comp_keys}
    for label, br in bridges.items():
        r = group if label == "集团" else rounded[label]
        pure = _round_half_up(br.pure_volume)
        mix = _round_half_up(br.mix)
        out.append("| " + " | ".join([
            f"**{label}**" if label == "集团" else label,
            yuan(r["volume"]), f"({yuan(pure)})", f"({yuan(mix)})",
            yuan(r["price"]), yuan(r["activity"]), yuan(r["subsidy"]), yuan(r["cost"]),
            yuan(r["interaction"]), yuan(r["structural"]), yuan(r["freight"]),
            yuan(r["refund"]), yuan(r["total"]),
        ]) + " |")
    if pool_delta is not None:
        out.append(f"| 待分运输池变化(仅集团) |  |  |  |  |  |  |  |  |  |  |  | {syuan(-pool_delta)} |")
        out.append(f"| **集团毛利差(含待分)** |  |  |  |  |  |  |  |  |  |  |  | **{syuan(group['total'] - pool_delta)}** |")
    out.append("")
    return "\n".join(out) + "\n"


def _bridge_row_rounded(br: Bridge) -> dict[str, int]:
    """各因子取整分;交互吸收取舍残差,保证行内合计分毫不差。"""
    r = {k: _round_half_up(v) for k, v in [
        ("volume", br.volume), ("price", br.price), ("activity", br.activity),
        ("subsidy", br.subsidy), ("cost", br.cost),
        ("structural", br.structural), ("freight", br.freight_change),
        ("refund", br.refund_change)]}
    total = _round_half_up(br.delta_margin)
    r["interaction"] = total - sum(r.values())
    r["total"] = total
    return r


def structural_section(br: Bridge, sku_name: dict[str, str]) -> str:
    if not br.structural_detail:
        return ""
    out = ["### 结构进出明细(同口径不可比,不进价格/销量因子)", "",
           "| 格子(门店·规格·渠道) | 方向 | 毛利影响(元) |", "| --- | --- | --- |"]
    for (store, sku, channel), direction, amount in br.structural_detail:
        name = sku_name.get(sku, sku)
        out.append(f"| {store}·{sku}({name})·{channel} | {direction} | {syuan(amount)} |")
    out.append("")
    return "\n".join(out) + "\n"


def cash_section(ledger: Ledger, city: str | None = None) -> str:
    rows = [r for r in ledger.cash_refunds if city is None or r.city == city]
    out = ["## 退款:销售周口径 × 现金周口径", "",
           "退款冲减原销售观察周;现金流出落在实际付款所在现金周。两口径同总额,用途不同,不可混用。",
           "", "| 现金周 \\ 销售周 | W1 | W2 | W3 | 现金流出合计 |", "| --- | --- | --- | --- | --- |"]
    weeks = ["W1", "W2", "W3"]
    for cw in weeks:
        cells = []
        for sw in weeks:
            cells.append(sum(r.cash_out_cents for r in rows if r.cash_week == cw and r.sale_week == sw))
        total = sum(cells)
        out.append(f"| {cw} | " + " | ".join(yuan(c) for c in cells) + f" | {yuan(total)} |")
    accr = [sum(r.cash_out_cents for r in rows if r.sale_week == sw) for sw in weeks]
    out.append("| 同一批退款按销售周归集(权责口径) | " + " | ".join(yuan(c) for c in accr) + " | — |")
    out.append("")
    return "\n".join(out) + "\n"


def freight_section(ledger: Ledger, prev: Ledger | None) -> str:
    """行程级运费明细,仅集团(财务)版。"""
    out = ["## 运费行程明细(仅财务可见)", "",
           "| 行程 | 版本 | 总价(元) | 已知时刻 | 已分(元) | 待分(元) | 缺重行 |",
           "| --- | --- | --- | --- | --- | --- | --- |"]
    alloc: dict[str, int] = {}
    for l in ledger.lines:
        if l.freight_cents is not None:
            alloc[l.trip] = alloc.get(l.trip, 0) + l.freight_cents
    pools = {p.trip: p for p in ledger.pools}
    for trip, (rev, cents, known_at) in sorted(ledger.freight_version.items()):
        pool = pools.get(trip)
        miss = ",".join(pool.missing_lines) if pool else ""
        out.append(f"| {trip} | r{rev} | {yuan(cents)} | {known_at} | {yuan(alloc.get(trip, 0))} "
                   f"| {yuan(pool.cents) if pool else '0.00'} | {miss} |")
    out.append("")
    if prev is not None:
        for trip, (rev, cents, _) in sorted(ledger.freight_version.items()):
            old = prev.freight_version.get(trip)
            if old and old[0] != rev:
                out.append(f"- 补账:{trip} 版本 r{old[0]}→r{rev},{yuan(old[1])}→{yuan(cents)} 元"
                           f"(差额 {syuan(cents - old[1])}),已按同一行程全部订单重分摊。")
        out.append("")
    return "\n".join(out) + "\n"


def items_section(items: list[Item], dispositions: list[RecapDisposition],
                  city: str | None, city_contact: str | None,
                  impacts: dict[tuple[str, str], dict[str, str]]) -> str:
    """待查事项。city=None 为集团版;否则只列本城待办与关联,金额只给本城口径。

    主办一律是负责解决根因的人(不按账面降幅指派);本城欠回执的重送件算本城待办,
    其余本城只认关联、不派工。
    """
    def owed_receipt(it: Item, c: str) -> bool:
        return any(d.recap.recap in it.recap_ids and d.recap.scope == c
                   and d.disposition == "重送同一期" for d in dispositions)

    own, related = [], []
    for it in items:
        if it.status == "关闭":
            continue
        owner = it.finding.owner
        if city is None:
            own.append((it, owner))
        elif owed_receipt(it, city) or owner == city_contact:
            own.append((it, owner))
        elif city in it.finding.related_cities:
            related.append((it, owner))
    out = ["## 待查事项" + ("" if city is None else "(本城)"), ""]
    if city is None:
        out += ["同一费用只开一件,由负责解决根因的人接手;其他视角认作关联,不因跨切片反复派工。", ""]
        out += ["| 事项 | 根因 | 状态 | 主办 | 金额(元) | 关联 | 旧登记 | 对应业务动作 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for it, owner in own:
            f = it.finding
            recaps = "、".join(it.recap_ids) or "—"
            out.append(f"| {f.kind} | {f.root[1]} | {it.status} | {owner} | {syuan(f.amount_cents)} "
                       f"| {'、'.join(f.related_cities)} | {recaps} | {f.action} |")
        out.append("")
        closed = [it for it in items if it.status == "关闭"]
        for it in closed:
            out.append(f"- 已关闭:{it.finding.kind} {it.finding.root[1]}——{it.note}")
        if closed:
            out.append("")
    else:
        if own:
            out += ["### 本城待办", "", "| 事项 | 主办 | 状态 | 本城影响 | 对应业务动作 |",
                    "| --- | --- | --- | --- | --- |"]
            for it, owner in own:
                impact = impacts.get((it.finding.root[0], it.finding.root[1]), {}).get(city, "—")
                out.append(f"| {it.finding.kind}({it.finding.root[1]}) | {owner} | {it.status} | {impact} | {it.finding.action} |")
            out.append("")
        if related:
            out += ["### 关联事项(他方主办,本城只知情,不派工)", "",
                    "| 事项 | 主办 | 状态 | 本城影响 |", "| --- | --- | --- | --- |"]
            for it, owner in related:
                impact = impacts.get((it.finding.root[0], it.finding.root[1]), {}).get(city, "—")
                out.append(f"| {it.finding.kind}({it.finding.root[1]}) | {owner} | {it.status} | {impact} |")
            out.append("")
        if not own and not related:
            out += ["本期无本城待办。", ""]
    return "\n".join(out) + "\n"


def dispositions_section(dispositions: list[RecapDisposition]) -> str:
    out = ["## 旧复盘登记处置", "",
           "| 登记 | 范围 | 原指派根因 | 原主办 | 处置 | 说明 |", "| --- | --- | --- | --- | --- | --- |"]
    for d in dispositions:
        r = d.recap
        out.append(f"| {r.recap} | {r.scope} | {r.assigned_root} | {r.owner} | {d.disposition} | {d.note} |")
    out.append("")
    return "\n".join(out) + "\n"


def slice_tables(ledger: Ledger, week: str) -> str:
    """财务版附表:按渠道、按规格的同口径切片,供任意组合下钻。"""
    out = [f"## 切片附表({week},集团口径,单位:元)", ""]
    from .bridge import week_parts
    for title, key, vals in [("按渠道", "channel", CHANNELS), ("按规格", "sku", SKUS)]:
        out += [f"### {title}", "", "| 切片 | 成交额 | 外部补贴 | 自担券 | 商品成本 | 已分运输 | 退款净冲减 | 毛利 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for v in vals:
            p = week_parts(ledger.lines_of(week=week, **{key: v}))
            out.append(f"| {v} | {yuan(p.net_sales)} | {yuan(p.subsidy)} | {yuan(p.coupon)} "
                       f"| {yuan(-p.product_cost)} | {yuan(-p.freight_alloc)} | {yuan(-p.refund_net)} | {yuan(p.margin)} |")
        out.append("")
    return "\n".join(out) + "\n"
