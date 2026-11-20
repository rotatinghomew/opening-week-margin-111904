"""编排:按切点出周报(集团财务版 + 分城经营版),并出新旧周报并排对比。"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import render
from .bridge import Bridge, build_bridge, week_parts
from .findings import Finding, Item, RecapDisposition, collect_findings, reconcile
from .ledger import Ledger, build_ledger
from .model import CITIES, Source, load_source

OUT = Path(__file__).resolve().parents[1] / "out"


@dataclass
class CutResult:
    src: Source
    cut: str
    ledger: Ledger
    weeks: tuple[str, str]
    bridges: dict[str, Bridge]
    findings: list[Finding]
    items: list[Item]
    dispositions: list[RecapDisposition]
    impacts: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)


def _freight_impacts(src: Source, ledger: Ledger, prev: Ledger | None,
                     findings: list[Finding]) -> dict[tuple[str, str], dict[str, str]]:
    """各城对运费类根因的本城影响口径(只含本城数字,可供经营版披露)。"""
    impacts: dict[tuple[str, str], dict[str, str]] = {}
    trips = {f.root[1] for f in findings if f.root[0] == "运费"}
    pool_trips = {p.trip for p in ledger.pools}
    for trip in trips:
        per_city: dict[str, str] = {}
        for city in CITIES:
            cur = [l for l in ledger.lines_of(city=city) if l.trip == trip]
            pending = sum(1 for l in cur if l.freight_cents is None)
            allocated = sum(1 for l in cur if l.freight_cents is not None)
            parts = []
            if pending:
                parts.append(f"{pending}行运费待定")
            if trip in pool_trips and allocated:
                parts.append(f"{allocated}行暂按已登记重量分摊,重量到齐后或调整")
            if prev is not None:
                old = {l.line: l.freight_cents for l in prev.lines
                       if l.trip == trip and l.city == city}
                delta = sum((l.freight_cents or 0) - (old.get(l.line) or 0) for l in cur)
                if delta:
                    parts.append(f"本城分摊调整{render.syuan(delta)}元")
            per_city[city] = ";".join(parts) if parts else "—"
        impacts[("运费", trip)] = per_city
    for f in findings:
        if f.root[0] == "活动券":
            city = f.root[1]
            impacts.setdefault(("活动券", city), {})[city] = f"自担券周环比{render.syuan(f.amount_cents)}元"
    return impacts


def build_cut(src: Source, cut: str, prev: CutResult | None) -> CutResult:
    ledger = build_ledger(src, cut)
    weeks = (src.policy.weeks[0].name, src.policy.weeks[1].name)
    bridges: dict[str, Bridge] = {"集团": build_bridge(ledger, weeks[0], weeks[1], "集团")}
    for city in CITIES:
        bridges[city] = build_bridge(ledger, weeks[0], weeks[1], city, city=city)
    findings = collect_findings(src, ledger, prev.ledger if prev else None, weeks[0], weeks[1])
    items, dispositions = reconcile(src, findings, prev.findings if prev else None)
    impacts = _freight_impacts(src, ledger, prev.ledger if prev else None, findings)
    return CutResult(src=src, cut=cut, ledger=ledger, weeks=weeks, bridges=bridges,
                     findings=findings, items=items, dispositions=dispositions, impacts=impacts)


def render_finance(cr: CutResult, prev: CutResult | None) -> str:
    src, ledger = cr.src, cr.ledger
    trip_week = {l.trip: l.week for l in ledger.lines}
    pool_delta = None
    if prev is None:
        w = cr.weeks
        pool_delta = (sum(p.cents for p in ledger.pools if trip_week.get(p.trip) == w[1])
                      - sum(p.cents for p in ledger.pools if trip_week.get(p.trip) == w[0]))
    w0, w1 = cr.weeks
    channel_bridges = {ch: build_bridge(ledger, w0, w1, ch, channel=ch)
                       for ch in ["线上", "柜台"]}
    sku_bridges = {s: build_bridge(ledger, w0, w1, s, sku=s) for s in ["S", "L", "M"]}
    doc = [render.header("集团财务周报(仅财务)", cr.cut,
                         f"报送节奏:{src.policy.weekly_local_time};异动参考线 {src.policy.look_at_cents} 分。"),
           render.checks_section(ledger),
           render.week_table(ledger, cr.weeks, CITIES, group=True),
           render.bridge_table(cr.bridges, pool_delta=pool_delta),
           render.structural_section(cr.bridges["集团"], src.sku_name),
           "### 同口径切片:按渠道\n\n" + render.bridge_table(channel_bridges, pool_delta=None, heading=False),
           "### 同口径切片:按规格\n\n" + render.bridge_table(sku_bridges, pool_delta=None, heading=False),
           render.cash_section(ledger),
           render.freight_section(ledger, prev.ledger if prev else None),
           render.items_section(cr.items, cr.dispositions, None, None, cr.impacts),
           render.dispositions_section(cr.dispositions),
           render.slice_tables(ledger, cr.weeks[1]),
           _deliveries_section(cr)]
    return "\n".join(doc)


def _deliveries_section(cr: CutResult) -> str:
    out = ["## 本期投递与回执", "",
           "经营版按城投递,只含本城可披露汇总与本城待办;集团版只供财务。回执未到的重送同一期,不宣布签收。",
           "", "| 接收方 | 版本 | 内容 | 回执 |", "| --- | --- | --- | --- |"]
    out.append(f"| {cr.src.policy.contacts['财务']} | 集团版 | 全量(含待分池与行程明细) | 财务内部 |")
    store_city = {"A": "西城", "B": "北城", "C": "南城"}
    for store, contact in sorted(cr.src.store_contact.items()):
        city = store_city[store]
        own = sum(1 for it in cr.items if it.status != "关闭" and city in it.finding.related_cities)
        ack = "、".join(f"{d.recap.recap}:{d.recap.delivered_ack}" for d in cr.dispositions
                        if d.recap.scope in (city, "全部"))
        resend = any(d.disposition == "重送同一期" and d.recap.scope in (city, "全部")
                     for d in cr.dispositions)
        out.append(f"| {contact}({city}) | 经营版 | 本城汇总+待办/关联{own}件"
                   f"{'(含重送同一期)' if resend else ''} | {ack or '—'} |")
    out.append("")
    return "\n".join(out) + "\n"


def render_city(cr: CutResult, city: str) -> str:
    ledger = cr.ledger
    contact = [c for s, c in cr.src.store_contact.items()
               if {"A": "西城", "B": "北城", "C": "南城"}[s] == city][0]
    note = ("\n> 本版只含本城可披露汇总与本城待办;跨城待分费用由财务统一汇总,不在本版披露;"
            "本版不提供全公司合计,请勿用手工拼列方式还原全公司数。\n")
    doc = [render.header(f"{city}经营版周报(接收:{contact})", cr.cut, note),
           render.week_table(ledger, cr.weeks, [city], group=False),
           render.bridge_table({city: cr.bridges[city]}, pool_delta=None),
           render.structural_section(cr.bridges[city], cr.src.sku_name),
           render.cash_section(ledger, city=city),
           render.items_section(cr.items, cr.dispositions, city, contact, cr.impacts)]
    return "\n".join(doc)


def render_compare(old: CutResult, new: CutResult) -> str:
    """新旧周报并排:每部分差额落在哪,哪些仍无依据。"""
    doc = [f"# 新旧周报并排对比(仅财务)\n",
           f"旧周报切点:{old.cut};新周报切点:{new.cut}。两周观察期不变,"
           f"差额全部来自切点间新到的资料(运费补账、计费重量补登记、新到账退款)。", ""]
    # 1) 组件级落点
    doc += ["## 每部分差额落在哪(新−旧,元)", "",
            "| 周 | 组件 | " + " | ".join(CITIES + ["待分池", "集团"]) + " | 落点 |",
            "| --- | --- |" + " --- |" * (len(CITIES) + 2) + " --- |"]
    causes = _component_deltas(old, new)
    for week in old.weeks:
        for comp, label in [("net_sales", "客户成交额"), ("subsidy", "外部补贴"),
                            ("coupon", "自担券"), ("product_cost", "商品成本"),
                            ("freight_alloc", "已分运输"), ("refund_net", "退款净冲减"),
                            ("margin", "毛利(已分口径)")]:
            row = causes[week][comp]
            total = sum(row[c] for c in CITIES)
            cause = _cause_of(comp, row, old, new)
            doc.append(f"| {week} | {label} | " + " | ".join(syuan0(row[c]) for c in CITIES)
                       + f" | — | {syuan0(total)} | {cause} |")
        pool_old = old.ledger.pool_cents()
        pool_new = new.ledger.pool_cents()
        pool_delta = (pool_new - pool_old) if week == old.weeks[-1] else 0
        margin_delta = sum(causes[week]["margin"][c] for c in CITIES)
        doc.append(f"| {week} | 待分运输成本 | " + " | ".join(["—"] * len(CITIES))
                   + f" | {syuan0(pool_delta)} | — | "
                   + ("待分池随计费重量到齐消化,落回行程分摊" if week == old.weeks[-1] else "—") + " |")
        doc.append(f"| {week} | **毛利(含待分口径)** | " + " | ".join(["—"] * len(CITIES))
                   + f" | — | **{syuan0(margin_delta - pool_delta)}** | "
                   + ("待分消化只是换列,金额未变,不算新损失" if week == old.weeks[-1] else "—") + " |")
    doc.append("")
    # 2) 行级落点明细
    doc += ["## 行级落点明细", "",
            "| 周 | 城市 | 原因 | 毛利影响(元) |", "| --- | --- | --- | --- |"]
    for week, city, cause, amount in _line_level_landings(old, new):
        doc.append(f"| {week} | {city} | {cause} | {syuan0(amount)} |")
    doc.append("")
    # 3) 仍无依据
    doc += ["## 仍无依据/未结事项", ""]
    pending = [d for d in new.dispositions if d.disposition == "重送同一期"]
    for d in pending:
        doc.append(f"- {d.recap.recap}({d.recap.scope},{d.recap.assigned_root}):回执未到,已重送同一期,"
                   f"未获确认前不得宣布复盘已签收。")
    open_items = [it for it in new.items if it.status in ("在办", "重送", "新开")]
    for it in open_items:
        doc.append(f"- {it.finding.kind}({it.finding.root[1]}):{it.status},主办 "
                   f"{it.finding.owner};{it.finding.detail}")
    if not pending and not open_items:
        doc.append("- 无。")
    doc.append("")
    doc.append(render.dispositions_section(new.dispositions))
    return "\n".join(doc)


def syuan0(cents: int) -> str:
    return render.yuan(cents, sign=True) if cents else "0.00"


def _component_deltas(old: CutResult, new: CutResult) -> dict:
    out = {}
    for week in old.weeks:
        comps = {}
        for comp in ["net_sales", "subsidy", "coupon", "product_cost",
                     "freight_alloc", "refund_net", "margin"]:
            row = {}
            for city in CITIES:
                p_old = week_parts(old.ledger.lines_of(week=week, city=city))
                p_new = week_parts(new.ledger.lines_of(week=week, city=city))
                row[city] = getattr(p_new, comp) - getattr(p_old, comp)
            comps[comp] = row
        out[week] = comps
    return out


def _cause_of(comp: str, row: dict[str, int], old: CutResult, new: CutResult) -> str:
    if not any(row.values()):
        return "无变化"
    if comp == "freight_alloc":
        changed = [t for t, (rev, _, _) in new.ledger.freight_version.items()
                   if t in old.ledger.freight_version and old.ledger.freight_version[t][0] != rev]
        return f"运费补账重分摊({'、'.join(sorted(changed))})" if changed else "运费分摊变化"
    if comp == "refund_net":
        return "退款新到账(现金落在其实际现金周)"
    if comp == "margin":
        return "上述各项合计"
    return "随运费/退款变化的联动"


def _line_level_landings(old: CutResult, new: CutResult) -> list[tuple[str, str, str, int]]:
    out = []
    changed_trips = [t for t, (rev, _, _) in new.ledger.freight_version.items()
                     if t in old.ledger.freight_version and old.ledger.freight_version[t][0] != rev]
    for week in old.weeks:
        for city in CITIES:
            for trip in changed_trips:
                o = sum(l.freight_cents or 0 for l in old.ledger.lines
                        if l.week == week and l.city == city and l.trip == trip)
                n = sum(l.freight_cents or 0 for l in new.ledger.lines
                        if l.week == week and l.city == city and l.trip == trip)
                if n - o:
                    out.append((week, city, f"运费{trip}补账重分摊(本城份额)", -(n - o)))
            o = sum(l.refund_net_cents for l in old.ledger.lines if l.week == week and l.city == city)
            n = sum(l.refund_net_cents for l in new.ledger.lines if l.week == week and l.city == city)
            if n - o:
                out.append((week, city, "退款新到账冲减原销售周", -(n - o)))
    pool_delta = new.ledger.pool_cents() - old.ledger.pool_cents()
    if pool_delta:
        out.append((old.weeks[-1], "集团(待分池)", "待分消化落回行程分摊(口径内转移,非新损失)", -pool_delta))
    return out


def write_csvs(cr: CutResult, tag: str) -> None:
    ledger_path = OUT / f"ledger_{tag}.csv"
    with ledger_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["line", "order", "week", "city", "store", "channel", "sku", "trip",
                    "units", "list_cents", "coupon_cents", "subsidy_cents",
                    "product_cost_cents", "freight_cents", "refund_cash_cents",
                    "refund_recovered_cents"])
        for l in cr.ledger.lines:
            w.writerow([l.line, l.order, l.week, l.city, l.store, l.channel, l.sku, l.trip,
                        l.units, l.list_cents, l.coupon_cents, l.subsidy_cents,
                        l.product_cost_cents,
                        "" if l.freight_cents is None else l.freight_cents,
                        l.refund_cash_cents, l.refund_recovered_cents])
    items_path = OUT / f"items_{tag}.json"
    items_path.write_text(json.dumps([
        {"root": list(it.finding.root), "kind": it.finding.kind, "status": it.status,
         "owner": it.finding.owner, "amount_cents": it.finding.amount_cents,
         "related_cities": list(it.finding.related_cities), "action": it.finding.action,
         "recap_ids": list(it.recap_ids), "note": it.note}
        for it in cr.items], ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    src = load_source()
    cut1, cut2 = src.policy.cuts
    cr1 = build_cut(src, cut1, None)
    cr2 = build_cut(src, cut2, cr1)

    (OUT / "集团财务周报_cut1.md").write_text(render_finance(cr1, None), encoding="utf-8")
    (OUT / "集团财务周报_cut2.md").write_text(render_finance(cr2, cr1), encoding="utf-8")
    for city in CITIES:
        (OUT / f"经营版周报_{city}_cut1.md").write_text(render_city(cr1, city), encoding="utf-8")
        (OUT / f"经营版周报_{city}_cut2.md").write_text(render_city(cr2, city), encoding="utf-8")
    (OUT / "新旧周报并排对比_仅财务.md").write_text(render_compare(cr1, cr2), encoding="utf-8")
    write_csvs(cr1, "cut1")
    write_csvs(cr2, "cut2")
    print("已生成:", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
