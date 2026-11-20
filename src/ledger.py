"""截至某一资料切点(cut)的周毛利总账。

铁律:
1. 同一行程的运费只支出一次,按业务登记的计费重量分摊;分摊 + 待分 = 行程总价,分毫不差。
2. 行程总价已知而部分计费重量未登记时,待分部分独列为「待分运输成本」;
   不得在已知行上分完、再给无资料行报零——无资料行的运费是「待定」而不是 0。
3. 退款冲减原销售观察周;现金流出单列在其实际现金周。两套口径都出数,互不相混。
4. 空值不做零:缺重量的行进待分池,不静默按 0 处理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from statistics import median

from .model import Source, SaleLine


@dataclass(frozen=True)
class LineFact:
    """一行销售在某个切点下的全部口径事实。金额单位:分。"""

    line: str
    order: str
    week: str            # 销售观察周
    sold_day: str
    city: str
    store: str
    channel: str
    sku: str
    trip: str
    units: int
    list_cents: int
    coupon_cents: int            # 自担活动费
    subsidy_cents: int           # 外部补贴(收入)
    product_cost_cents: int
    freight_cents: int | None    # None = 计费重量未登记,运费待定(不是 0)
    refund_cash_cents: int       # 切点内可见退款,冲减本行销售周收入
    refund_recovered_cents: int  # 回收商品冲减本行销售周成本

    @property
    def net_sales_cents(self) -> int:
        return self.list_cents - self.coupon_cents

    @property
    def margin_ex_freight_cents(self) -> int:
        """不含运费、不含退款的行毛利(用于差异拆解的销量/售价等因子)。"""
        return self.list_cents - self.coupon_cents + self.subsidy_cents - self.product_cost_cents

    @property
    def refund_net_cents(self) -> int:
        """退款对销售周毛利的净冲减(正数=减少毛利)。"""
        return self.refund_cash_cents - self.refund_recovered_cents


@dataclass(frozen=True)
class PoolItem:
    """待分运输成本:行程总价已知、部分计费重量未登记而暂时落不下去的部分。"""

    trip: str
    cents: int
    known_lines: tuple[str, ...]
    missing_lines: tuple[str, ...]
    est_kg_per_missing: tuple[tuple[str, Fraction], ...]  # 每缺重行的估计重量(口径见 build_ledger)
    trip_cents: int
    cross_city: bool


@dataclass(frozen=True)
class CashRefund:
    refund: str
    line: str
    sale_week: str
    cash_week: str
    city: str
    cash_out_cents: int
    recovered_cents: int


@dataclass
class Ledger:
    cut: str
    lines: list[LineFact]
    pools: list[PoolItem]
    cash_refunds: list[CashRefund]          # 切点内可见的全部退款(现金口径)
    freight_version: dict[str, tuple[int, int, str]]  # trip -> (rev, cents, known_at)
    kg_of_line: dict[str, int]              # 切点内可见的计费重量
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def lines_of(self, week: str | None = None, city: str | None = None,
                 channel: str | None = None, sku: str | None = None,
                 store: str | None = None) -> list[LineFact]:
        out = self.lines
        if week is not None:
            out = [l for l in out if l.week == week]
        if city is not None:
            out = [l for l in out if l.city == city]
        if channel is not None:
            out = [l for l in out if l.channel == channel]
        if sku is not None:
            out = [l for l in out if l.sku == sku]
        if store is not None:
            out = [l for l in out if l.store == store]
        return out

    def pool_cents(self, trip: str | None = None) -> int:
        return sum(p.cents for p in self.pools if trip is None or p.trip == trip)


def _largest_remainder(shares: dict[str, Fraction], target: int) -> dict[str, int]:
    """把 target 分按份额比例分成整数分,保证合计恰好等于 target。"""
    floors = {k: int(v) for k, v in shares.items()}
    rest = target - sum(floors.values())
    order = sorted(shares, key=lambda k: (shares[k] - int(shares[k]), k), reverse=True)
    out = dict(floors)
    for k in order[:rest]:
        out[k] += 1
    return out


def build_ledger(src: Source, cut: str) -> Ledger:
    policy = src.policy
    # 1) 切点可见的运费版本:每趟取 known_at <= cut 的最高 rev;后到版本是全额。
    freight_version: dict[str, tuple[int, int, str]] = {}
    for f in src.freight:
        if f.known_at <= cut:
            cur = freight_version.get(f.trip)
            if cur is None or f.rev > cur[0]:
                freight_version[f.trip] = (f.rev, f.cents, f.known_at)
    # 2) 切点可见的计费重量。
    kg_of_line: dict[str, int] = {}
    for b in src.billable:
        if b.known_at <= cut:
            kg_of_line[b.line] = b.chargeable_kg
    # 3) 缺重行的估计重量:同规格可见行的每公斤件数中位数 × 本行件数。
    #    只用于把「待分」与「已分」切开,估计部分全部进待分池,不摊到任何已知行。
    kg_per_unit: dict[str, float] = {}
    sale_by_line = {s.line: s for s in src.sales}
    for sku in {s.sku for s in src.sales}:
        rates = [kg_of_line[s.line] / s.units for s in src.sales
                 if s.sku == sku and s.line in kg_of_line and s.units > 0]
        kg_per_unit[sku] = median(rates) if rates else 0.0

    lines: list[LineFact] = []
    pools: list[PoolItem] = []
    by_trip: dict[str, list[SaleLine]] = {}
    for s in src.sales:
        by_trip.setdefault(s.trip, []).append(s)

    freight_alloc: dict[str, int] = {}
    for trip, trip_lines in sorted(by_trip.items()):
        rev, cents, _ = freight_version[trip]
        known = {s.line: kg_of_line[s.line] for s in trip_lines if s.line in kg_of_line}
        missing = [s.line for s in trip_lines if s.line not in kg_of_line]
        if not missing:
            total_kg = sum(known.values())
            shares = {l: Fraction(cents) * kg / total_kg for l, kg in known.items()}
            freight_alloc.update(_largest_remainder(shares, cents))
            continue
        # 分母不全:已知行只拿自己重量对应的份额,缺重行的估计份额独列待分。
        est = {l: Fraction(str(kg_per_unit[sale_by_line[l].sku])) * sale_by_line[l].units
               for l in missing}
        known_kg = sum(known.values())
        est_kg = sum(est.values())
        rate = Fraction(cents) / (known_kg + est_kg)
        target_known = int(rate * known_kg + Fraction(1, 2))
        shares = {l: rate * kg for l, kg in known.items()}
        freight_alloc.update(_largest_remainder(shares, target_known))
        pool_cents = cents - target_known
        cities = {sale_by_line[l].city for l in list(known) + missing}
        pools.append(PoolItem(
            trip=trip, cents=pool_cents,
            known_lines=tuple(sorted(known)), missing_lines=tuple(sorted(missing)),
            est_kg_per_missing=tuple(sorted(est.items())), trip_cents=cents,
            cross_city=len(cities) > 1,
        ))

    # 4) 切点可见的退款:按实际付款时刻入账可见;冲减原销售周,现金单列。
    visible_refunds = [r for r in src.refunds if r.paid_at <= cut]
    refund_by_line: dict[str, tuple[int, int]] = {}
    cash_refunds: list[CashRefund] = []
    for r in visible_refunds:
        sale = sale_by_line[r.line]
        cash, recov = refund_by_line.get(r.line, (0, 0))
        refund_by_line[r.line] = (cash + r.cash_out_cents, recov + r.recovered_product_cost_cents)
        cash_refunds.append(CashRefund(
            refund=r.refund, line=r.line,
            sale_week=policy.week_of(sale.sold_day), cash_week=policy.week_of(r.paid_at[:10]),
            city=sale.city, cash_out_cents=r.cash_out_cents,
            recovered_cents=r.recovered_product_cost_cents,
        ))

    for s in src.sales:
        cash, recov = refund_by_line.get(s.line, (0, 0))
        lines.append(LineFact(
            line=s.line, order=s.order, week=policy.week_of(s.sold_day),
            sold_day=s.sold_day, city=s.city, store=s.store, channel=s.channel,
            sku=s.sku, trip=s.trip, units=s.units, list_cents=s.list_cents,
            coupon_cents=s.coupon_cents, subsidy_cents=s.outside_subsidy_cents,
            product_cost_cents=s.product_cost_cents,
            freight_cents=freight_alloc.get(s.line),
            refund_cash_cents=cash, refund_recovered_cents=recov,
        ))

    ledger = Ledger(cut=cut, lines=lines, pools=pools, cash_refunds=cash_refunds,
                    freight_version=freight_version, kg_of_line=kg_of_line)
    _self_check(src, ledger)
    return ledger


def _self_check(src: Source, ledger: Ledger) -> None:
    """对账校验:任何一条不过,周报不得发出。"""
    checks = ledger.checks
    # 守恒:每趟 已分 + 待分 = 行程总价
    by_trip_alloc: dict[str, int] = {}
    for l in ledger.lines:
        if l.freight_cents is not None:
            by_trip_alloc[l.trip] = by_trip_alloc.get(l.trip, 0) + l.freight_cents
    ok = True
    for trip, (rev, cents, _) in ledger.freight_version.items():
        alloc = by_trip_alloc.get(trip, 0)
        pool = ledger.pool_cents(trip)
        if alloc + pool != cents:
            ok = False
            checks.append((f"运费守恒-{trip}", False, f"已分{alloc}+待分{pool} != 总价{cents}"))
    checks.append(("运费守恒(每趟 已分+待分=总价)", ok, ""))
    # 无资料行不得报零:缺重量的行 freight 必须是 None,且对应有待分池
    no_data = [l for l in ledger.lines if l.line not in ledger.kg_of_line]
    checks.append(("无计费重量行运费为待定(None)而非0",
                   all(l.freight_cents is None for l in no_data), ""))
    pool_lines = {l for p in ledger.pools for l in p.missing_lines}
    checks.append(("无计费重量行全部挂在待分池", {l.line for l in no_data} <= pool_lines, ""))
    # 归属资格:销售日不得早于门店 valid_from
    bad = [l.line for l in ledger.lines
           if l.sold_day < src.store_valid_from[l.store]]
    checks.append(("归属门店均在生效期内", not bad, ",".join(bad[:5])))
    # 退款现金口径与权责口径同总额
    accrual = sum(l.refund_cash_cents for l in ledger.lines)
    cash = sum(r.cash_out_cents for r in ledger.cash_refunds)
    checks.append(("退款权责/现金同总额", accrual == cash, f"{accrual} vs {cash}"))
