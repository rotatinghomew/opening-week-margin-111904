"""周结体系不变量测试:能对账、不重复领差、披露隔离、一事一件。"""
from __future__ import annotations

import re
import unittest
from fractions import Fraction

from src.bridge import build_bridge, week_parts
from src.findings import collect_findings, reconcile
from src.ledger import build_ledger
from src.model import CITIES, CHANNELS, SKUS, load_source
from src.pipeline import build_cut, render_city, render_finance

SRC = load_source()
CUT1, CUT2 = SRC.policy.cuts
W1, W2 = SRC.policy.weeks[0].name, SRC.policy.weeks[1].name


def make_cuts():
    cr1 = build_cut(SRC, CUT1, None)
    cr2 = build_cut(SRC, CUT2, cr1)
    return cr1, cr2


CR1, CR2 = make_cuts()


class FreightInvariant(unittest.TestCase):
    def test_conservation_every_trip_both_cuts(self):
        for cr in (CR1, CR2):
            alloc = {}
            for l in cr.ledger.lines:
                if l.freight_cents is not None:
                    alloc[l.trip] = alloc.get(l.trip, 0) + l.freight_cents
            for trip, (rev, cents, _) in cr.ledger.freight_version.items():
                self.assertEqual(alloc.get(trip, 0) + cr.ledger.pool_cents(trip), cents,
                                 f"{cr.cut} {trip} 已分+待分 != 总价")

    def test_missing_weight_lines_are_pending_not_zero(self):
        pool = CR1.ledger.pools[0]
        self.assertEqual(pool.trip, "T111")
        self.assertEqual(set(pool.missing_lines), {"L1115", "L1117"})
        for l in CR1.ledger.lines:
            if l.line in pool.missing_lines:
                self.assertIsNone(l.freight_cents, "无资料行运费必须是待定,不能报零")
        self.assertGreater(pool.cents, 0)
        self.assertEqual(CR2.ledger.pools, [], "重量到齐后不得再有待分池")

    def test_freight_versions_as_of_cut(self):
        self.assertEqual(CR1.ledger.freight_version["T111"][1], 7140)
        self.assertEqual(CR2.ledger.freight_version["T111"][1], 18900)
        # 后到版本是全额:cut2 的 T111 已分合计恰为 18900
        alloc = sum(l.freight_cents or 0 for l in CR2.ledger.lines if l.trip == "T111")
        self.assertEqual(alloc, 18900)

    def test_no_freight_double_pay(self):
        # 同一行程费用只支出一次:已分行分摊合计不超过行程总价
        for cr in (CR1, CR2):
            for trip, (rev, cents, _) in cr.ledger.freight_version.items():
                alloc = sum(l.freight_cents or 0 for l in cr.ledger.lines if l.trip == trip)
                self.assertLessEqual(alloc, cents)


class SliceConsistency(unittest.TestCase):
    def test_any_slice_sums_to_company(self):
        for cr in (CR1, CR2):
            for week in (W1, W2):
                total = week_parts(cr.ledger.lines_of(week=week))
                for key, vals in [("city", CITIES), ("channel", CHANNELS), ("sku", SKUS)]:
                    parts = [week_parts(cr.ledger.lines_of(week=week, **{key: v})) for v in vals]
                    for f in ["units", "net_sales", "coupon", "subsidy", "product_cost",
                              "freight_alloc", "refund_net", "margin"]:
                        self.assertEqual(sum(getattr(p, f) for p in parts), getattr(total, f),
                                         f"{cr.cut} {week} 按{key}切片合计不等于集团")

    def test_bridge_exact_for_any_slice(self):
        for cr in (CR1, CR2):
            for city in [None] + CITIES:
                for channel in [None] + CHANNELS:
                    for sku in [None] + SKUS:
                        br = build_bridge(cr.ledger, W1, W2, "t",
                                          city=city, channel=channel, sku=sku)
                        self.assertEqual(br.factor_sum(), br.delta_margin,
                                         f"切片{city}/{channel}/{sku} 因子合计不等于毛利差")

    def test_slices_do_not_double_claim(self):
        # 城市、渠道、规格三种分区的可加因子都必须精确加回集团
        for cr in (CR1, CR2):
            company = build_bridge(cr.ledger, W1, W2, "集团")
            for key, vals in [("city", CITIES), ("channel", CHANNELS), ("sku", SKUS)]:
                parts = [build_bridge(cr.ledger, W1, W2, str(v), **{key: v}) for v in vals]
                for f in ["volume", "price", "activity", "subsidy", "cost", "interaction",
                          "structural", "freight_change", "refund_change", "delta_margin"]:
                    self.assertEqual(sum(getattr(p, f) for p in parts), getattr(company, f),
                                     f"{cr.cut} 按{key}切片的{f}重复领差")


class RefundCaliber(unittest.TestCase):
    def test_accrual_equals_cash_total(self):
        for cr, n_visible in [(CR1, 54), (CR2, 84)]:
            accrual = sum(l.refund_cash_cents for l in cr.ledger.lines)
            cash = sum(r.cash_out_cents for r in cr.ledger.cash_refunds)
            self.assertEqual(accrual, cash)
            self.assertEqual(len(cr.ledger.cash_refunds), n_visible)

    def test_refund_in_original_sale_week_but_cash_in_paid_week(self):
        sale_week_of = {l.line: l.week for l in CR2.ledger.lines}
        for r in CR2.ledger.cash_refunds:
            self.assertEqual(r.sale_week, sale_week_of[r.line], "退款必须冲减原销售观察周")
        # W2 销售、W3 付款的退款:权责在 W2,现金在 W3
        cross = [r for r in CR2.ledger.cash_refunds if r.sale_week == W2 and r.cash_week == "W3"]
        self.assertEqual(len(cross), 30)
        self.assertFalse(any(r.cash_week == "W3" for r in CR1.ledger.cash_refunds),
                         "cut1 不得提前看见 W3 现金周退款")


class StructuralCaliber(unittest.TestCase):
    def test_like_for_like_cells(self):
        br = CR1.bridges["集团"]
        self.assertEqual(set(br.comparable), {("A", "S", "线上"), ("B", "L", "柜台")})
        self.assertEqual(set(br.entered),
                         {("A", "S", "柜台"), ("B", "L", "线上"), ("C", "M", "柜台"), ("C", "M", "线上")})
        self.assertEqual(br.exited, [])

    def test_specs_never_merged_by_name(self):
        # 礼盒 M 与散卖 S/L 永远分规格;南城(C店)W1 无销售,属跨期开店
        cells = {(l.store, l.sku) for l in CR2.ledger.lines}
        self.assertEqual(cells, {("A", "S"), ("B", "L"), ("C", "M")})
        c_w1 = [l for l in CR2.ledger.lines if l.store == "C" and l.week == W1]
        self.assertEqual(c_w1, [])

    def test_store_validity(self):
        for l in CR2.ledger.lines:
            self.assertGreaterEqual(l.sold_day, SRC.store_valid_from[l.store])


class InvestigationDiscipline(unittest.TestCase):
    def test_one_item_per_fee(self):
        for cr in (CR1, CR2):
            t111 = [it for it in cr.items if it.finding.root == ("运费", "T111")]
            self.assertEqual(len(t111), 1, "同一费用造成的调查只开一件")
            self.assertEqual(t111[0].finding.owner, "仓务复核")
            self.assertEqual(set(t111[0].finding.related_cities), set(CITIES))

    def test_recap_carry_over(self):
        d2 = {d.recap.recap: d for d in CR2.dispositions}
        self.assertEqual(d2["R3"].disposition, "补充在办记录")
        self.assertEqual(d2["R1"].disposition, "补充已办记录")
        self.assertIn("不另立损失", d2["R1"].note)
        self.assertEqual(d2["R2"].disposition, "重送同一期")
        # 回执未到不得宣布签收
        self.assertNotEqual(SRC.recaps[1].delivered_ack, "确认")
        self.assertIn("不得宣布", d2["R2"].note)

    def test_pool_resolved_not_reopened(self):
        # cut2 待分池已消化:不得再开新的待分运输事项
        self.assertFalse(any(it.finding.kind == "待分运输" and it.status != "关闭"
                             for it in CR2.items))


class Disclosure(unittest.TestCase):
    def test_city_report_isolation(self):
        other = {"西城": ["北城", "南城"], "北城": ["西城", "南城"], "南城": ["西城", "北城"]}
        for cr in (CR1, CR2):
            for city in CITIES:
                doc = render_city(cr, city)
                for o in other[city]:
                    self.assertNotIn(o, doc, f"{city}经营版不得出现{o}")
                self.assertNotIn("集团", doc, "经营版不得带全公司合计")
                self.assertNotIn("待分运输成本(集团独列)", doc)
                # 不得出现行程总价,避免反推他城订单
                for trip, (rev, cents, _) in cr.ledger.freight_version.items():
                    self.assertNotIn(f"{cents / 100:,.2f}", doc, f"{city}版不得露行程{trip}总价")

    def test_finance_report_has_pool_and_trips(self):
        doc = render_finance(CR1, None)
        self.assertIn("待分运输成本(集团独列)", doc)
        self.assertIn("T111", doc)
        self.assertIn("12.60", doc)  # 待分池金额只在财务版

    def test_pool_amount_not_in_city_report(self):
        for city in CITIES:
            doc = render_city(CR1, city)
            self.assertNotIn("12.60", doc, "跨城待分费用只给财务汇总")


class CompareReport(unittest.TestCase):
    def test_margin_delta_with_pool_caliber(self):
        # 含待分口径:新旧周报毛利差 = 已分口径差 − 待分池变化(换列不算新损失)
        for week in (W1, W2):
            m_old = sum(week_parts(CR1.ledger.lines_of(week=week, city=c)).margin for c in CITIES)
            m_new = sum(week_parts(CR2.ledger.lines_of(week=week, city=c)).margin for c in CITIES)
            pool_old = sum(p.cents for p in CR1.ledger.pools
                           if any(l.trip == p.trip and l.week == week for l in CR1.ledger.lines))
            pool_new = sum(p.cents for p in CR2.ledger.pools
                           if any(l.trip == p.trip and l.week == week for l in CR2.ledger.lines))
            self.assertEqual((m_new - pool_new) - (m_old - pool_old),
                             (m_new - m_old) - (pool_new - pool_old))

    def test_new_evidence_lands_somewhere(self):
        # cut1→cut2 的 W2 毛利差全部有落点:运费补账 + 退款新到账
        m_old = sum(week_parts(CR1.ledger.lines_of(week=W2, city=c)).margin for c in CITIES)
        m_new = sum(week_parts(CR2.ledger.lines_of(week=W2, city=c)).margin for c in CITIES)
        freight_delta = sum((l2.freight_cents or 0) - (l1.freight_cents or 0)
                            for l1, l2 in zip(sorted(CR1.ledger.lines, key=lambda l: l.line),
                                              sorted(CR2.ledger.lines, key=lambda l: l.line))
                            if l1.week == W2)
        refund_delta = sum(l2.refund_net_cents - l1.refund_net_cents
                           for l1, l2 in zip(sorted(CR1.ledger.lines, key=lambda l: l.line),
                                             sorted(CR2.ledger.lines, key=lambda l: l.line))
                           if l1.week == W2)
        self.assertEqual(m_new - m_old, -(freight_delta + refund_delta))


if __name__ == "__main__":
    unittest.main()
