"""待查事项:同一费用只开一件,由负责解决根因的人接手,其他视角认作关联。

规则:
- 事项按根因键归并:("运费", 行程) / ("活动券", 城市)。同一费用造成的调查只有一件,
  不因跨城市、跨渠道、跨规格切片反复派工;受影响的切片记为「关联」,不另立任务。
- 旧复盘登记(recaps)是沟通记录,根因标识不假定成立。新证据指向同一根因而有新影响:
  补充原记录;指向不同根因:说明旧行动是否仍成立,另开需要追查的部分。
- 仅因拆解角度改变而金额未变,不算又出现一笔损失。
- 回执未到的,可以重送同一期;不得先宣布复盘已经签收。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .bridge import week_parts
from .ledger import Ledger
from .model import Source, Recap


@dataclass(frozen=True)
class Finding:
    root: tuple[str, str]      # 根因键
    kind: str                  # 待分运输 / 运费补账 / 活动费异动
    amount_cents: int
    owner: str                 # 负责解决根因的人
    related_cities: tuple[str, ...]  # 关联视角(只知情,不派工)
    action: str                # 对应的业务选择
    detail: str


@dataclass
class Item:
    """一件待查事项(同一根因只此一件;可挂多条旧复盘登记)。"""
    finding: Finding
    status: str                # 新开 / 在办 / 重送 / 关闭
    recap_ids: tuple[str, ...] = ()
    note: str = ""


@dataclass
class RecapDisposition:
    recap: Recap
    matched_root: tuple[str, str] | None
    disposition: str           # 补充在办 / 旧行动仍成立·改认根因 / 重送同一期 / 维持 / 关闭
    note: str


def _trip_cities(ledger: Ledger, trip: str) -> tuple[str, ...]:
    return tuple(sorted({l.city for l in ledger.lines if l.trip == trip}))


def collect_findings(src: Source, ledger: Ledger, prev: Ledger | None,
                     week0: str, week1: str) -> list[Finding]:
    out: list[Finding] = []
    contacts = src.policy.contacts
    # 1) 待分运输池:分母不全,天然待查,直到重量到齐
    for pool in ledger.pools:
        out.append(Finding(
            root=("运费", pool.trip), kind="待分运输", amount_cents=pool.cents,
            owner=contacts["仓务"], related_cities=_trip_cities(ledger, pool.trip),
            action="仓务复核并补登记计费重量;财务在重量到位后按同一行程重分摊,不摊给无关订单",
            detail=f"行程{pool.trip}总价{pool.trip_cents}分,缺{len(pool.missing_lines)}行计费重量,"
                   f"待分{pool.cents}分独列,已知行只承担自身重量份额",
        ))
    # 2) 运费补账:与上一切点相比行程版本差额
    if prev is not None:
        for trip, (rev, cents, known_at) in sorted(ledger.freight_version.items()):
            old = prev.freight_version.get(trip)
            if old is not None and old[0] != rev:
                delta = cents - old[1]
                out.append(Finding(
                    root=("运费", trip), kind="运费补账", amount_cents=delta,
                    owner=contacts["仓务"], related_cities=_trip_cities(ledger, trip),
                    action="按补账全额对该行程所有订单重分摊;核查是否计费重量问题,"
                           "受影响城市只认关联,不分别派工",
                    detail=f"行程{trip}版本{old[0]}→{rev},{old[1]}→{cents}分,差额{delta:+}分",
                ))
    # 3) 活动费异动:各城券支出周环比超过异动参考线
    for city in sorted({l.city for l in ledger.lines}):
        p0 = week_parts(ledger.lines_of(week=week0, city=city))
        p1 = week_parts(ledger.lines_of(week=week1, city=city))
        delta = p1.coupon - p0.coupon
        if abs(delta) >= src.policy.look_at_cents:
            out.append(Finding(
                root=("活动券", city), kind="活动费异动", amount_cents=delta,
                owner=contacts["活动"], related_cities=(city,),
                action="复盘该城促销券投放口径与核销权限,决定本期是否收券",
                detail=f"{city}自担券 {p0.coupon}→{p1.coupon}分,周环比{delta:+}分",
            ))
    return out


def recap_root(recap: Recap) -> tuple[str, str] | None:
    """旧登记的根因标识映射为根因键;不假定旧根因已成立。"""
    if re.fullmatch(r"T\d+", recap.assigned_root):
        return ("运费", recap.assigned_root)
    if recap.assigned_root.startswith("活动券"):
        return ("活动券", recap.scope)
    return None


def reconcile(src: Source, findings: list[Finding], prev_findings: list[Finding] | None
              ) -> tuple[list[Item], list[RecapDisposition]]:
    """把本期发现与旧复盘登记对齐:同一根因只一件;旧记录按情形补充、说明或重送。"""
    by_root: dict[tuple[str, str], Finding] = {}
    for f in findings:
        # 同一根因键只留一件(如同一行程的待分与补账,合并为一件,金额取最新影响)
        if f.root in by_root:
            cur = by_root[f.root]
            by_root[f.root] = Finding(
                root=f.root, kind=f"{cur.kind}→{f.kind}" if cur.kind != f.kind else cur.kind,
                amount_cents=f.amount_cents, owner=f.owner,
                related_cities=tuple(sorted(set(cur.related_cities) | set(f.related_cities))),
                action=f.action, detail=f"{cur.detail};{f.detail}")
        else:
            by_root[f.root] = f

    dispositions: list[RecapDisposition] = []
    attached: dict[tuple[str, str], list[Recap]] = {}
    for recap in src.recaps:
        root = recap_root(recap)
        finding = by_root.get(root) if root else None
        if finding is None:
            # 旧登记在本期没有对应发现:回执未到的一律重送同一期,不宣布签收
            if recap.delivered_ack != "确认":
                dispositions.append(RecapDisposition(
                    recap=recap, matched_root=root, disposition="重送同一期",
                    note="回执未到,按同一期重送;未获确认前不得宣布复盘已签收"))
            else:
                dispositions.append(RecapDisposition(
                    recap=recap, matched_root=root, disposition="维持",
                    note="本期无新增影响,旧记录维持"))
            continue
        attached.setdefault(root, []).append(recap)
        done = bool(recap.resolution.strip()) and "待" not in recap.resolution
        if done:
            # 已办事项来了同根因新影响:补充原记录;金额只是换角度归属时不算新损失
            dispositions.append(RecapDisposition(
                recap=recap, matched_root=root, disposition="补充已办记录",
                note=f"同一根因的新影响,补充原记录不另开一件;旧行动「{recap.resolution}」"
                     f"本身仍成立,差额根因归属{finding.kind};金额未变仅归属角度改变,不另立损失"))
        elif recap.delivered_ack != "确认":
            dispositions.append(RecapDisposition(
                recap=recap, matched_root=root, disposition="重送同一期",
                note="回执未到,按同一期重送;未获确认前不得宣布复盘已签收"))
        else:
            dispositions.append(RecapDisposition(
                recap=recap, matched_root=root, disposition="补充在办记录",
                note="同一根因的新影响,补充在办记录,仍由根因负责人一件到底"))

    items: list[Item] = []
    for root, finding in by_root.items():
        recaps = attached.get(root, [])
        if not recaps:
            items.append(Item(finding=finding, status="新开",
                              note="本期新发现,按根因负责人开立一件"))
        elif any(r.delivered_ack != "确认" for r in recaps):
            items.append(Item(finding=finding, status="重送",
                              recap_ids=tuple(r.recap for r in recaps),
                              note="回执未到,重送同一期"))
        else:
            items.append(Item(finding=finding, status="在办",
                              recap_ids=tuple(r.recap for r in recaps),
                              note="同一根因只此一件,其他视角认作关联"))

    # 上期有、本期消失的发现:关闭并说明去向(如待分池已随重量到齐消化)
    if prev_findings:
        now_roots = set(by_root)
        for pf in prev_findings:
            if pf.root not in now_roots:
                items.append(Item(finding=pf, status="关闭",
                                  note="本期已消化(计费重量到齐/版本已更新),差额落入对应行程分摊"))
    return items, dispositions
