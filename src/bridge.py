"""周毛利差异桥(W2 对 W1),同一口径支持 城市×渠道×规格 任意组合切片。

口径:
- 可比格子 = 同一(门店, sku, 渠道)两周都有交易。跨期开店、新渠道、新规格
  一律先判「结构进出」,不进价格/销量因子;礼盒与散卖按 sku 区分,绝不并名。
- 因子(对不含运费、不含退款的行毛利 m = 标价 - 券 + 外部补贴 - 商品成本):
    销量   = Σ ΔQ·m0
    售价   = Σ Q0·Δp标价
    活动费 = −Σ Q0·Δ券      (自担促销,内部优惠)
    补贴   = +Σ Q0·Δ外部补贴 (外部收入,与内部优惠不同性质)
    成本   = −Σ Q0·Δ单位成本
    交互   = Σ ΔQ·Δm        (量率交叉,单列一项,不硬塞进价格或销量)
- 「结构」是销量的其中项:按本切片内部份额重排算出,只在本切片口径内有效,
  跨切片相加会重复领差,报表中明确标注不可跨片相加。
- 运输变化、退款变化为可认定事项,单列;待分运输池只进集团(财务)口径。
- 全部用分数精确计算,任何切片的因子合计都恰好等于该片毛利差,分毫不差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .ledger import Ledger, LineFact

Cell = tuple[str, str, str]  # (store, sku, channel)


@dataclass(frozen=True)
class WeekParts:
    """某周某切片的总账组件(分,整数)。"""
    units: int
    net_sales: int       # 成交额 = 标价 - 券
    list_sales: int
    coupon: int          # 自担活动费
    subsidy: int         # 外部补贴(收入)
    product_cost: int
    freight_alloc: int   # 已分运输
    freight_pending_lines: int  # 运费待定行数(金额在待分池)
    refund_net: int      # 退款净冲减
    margin: int          # 已分口径毛利 = 成交额+补贴-成本-已分运输-退款净冲减


def week_parts(lines: list[LineFact]) -> WeekParts:
    freight = sum(l.freight_cents for l in lines if l.freight_cents is not None)
    pending = sum(1 for l in lines if l.freight_cents is None)
    refund_net = sum(l.refund_net_cents for l in lines)
    net = sum(l.net_sales_cents for l in lines)
    subsidy = sum(l.subsidy_cents for l in lines)
    cost = sum(l.product_cost_cents for l in lines)
    return WeekParts(
        units=sum(l.units for l in lines),
        net_sales=net,
        list_sales=sum(l.list_cents for l in lines),
        coupon=sum(l.coupon_cents for l in lines),
        subsidy=subsidy,
        product_cost=cost,
        freight_alloc=freight,
        freight_pending_lines=pending,
        refund_net=refund_net,
        margin=net + subsidy - cost - freight - refund_net,
    )


@dataclass
class CellStat:
    units: int = 0
    list_cents: int = 0
    coupon_cents: int = 0
    subsidy_cents: int = 0
    cost_cents: int = 0
    freight_cents: int = 0
    refund_net_cents: int = 0

    @property
    def margin_ex(self) -> int:
        return self.list_cents - self.coupon_cents + self.subsidy_cents - self.cost_cents


def _cells(lines: list[LineFact]) -> dict[Cell, CellStat]:
    out: dict[Cell, CellStat] = {}
    for l in lines:
        key = (l.store, l.sku, l.channel)
        st = out.setdefault(key, CellStat())
        st.units += l.units
        st.list_cents += l.list_cents
        st.coupon_cents += l.coupon_cents
        st.subsidy_cents += l.subsidy_cents
        st.cost_cents += l.product_cost_cents
        st.freight_cents += l.freight_cents or 0
        st.refund_net_cents += l.refund_net_cents
    return out


@dataclass
class Bridge:
    """一切片的差异桥。金额均为 Fraction(分),合计精确相等。"""
    label: str
    comparable: list[Cell] = field(default_factory=list)
    entered: list[Cell] = field(default_factory=list)   # 结构新进
    exited: list[Cell] = field(default_factory=list)    # 结构退出
    volume: Fraction = Fraction(0)
    price: Fraction = Fraction(0)
    activity: Fraction = Fraction(0)
    subsidy: Fraction = Fraction(0)
    cost: Fraction = Fraction(0)
    interaction: Fraction = Fraction(0)
    pure_volume: Fraction = Fraction(0)   # 销量其中:纯量
    mix: Fraction = Fraction(0)           # 销量其中:结构(仅本切片口径)
    structural: Fraction = Fraction(0)    # 结构进出净额
    structural_detail: list[tuple[Cell, str, Fraction]] = field(default_factory=list)
    freight_change: Fraction = Fraction(0)
    refund_change: Fraction = Fraction(0)
    delta_margin: Fraction = Fraction(0)

    def factor_sum(self) -> Fraction:
        return (self.volume + self.price + self.activity + self.subsidy
                + self.cost + self.interaction + self.structural
                + self.freight_change + self.refund_change)


def build_bridge(ledger: Ledger, week0: str, week1: str, label: str,
                 city: str | None = None, channel: str | None = None,
                 sku: str | None = None) -> Bridge:
    lines0 = ledger.lines_of(week=week0, city=city, channel=channel, sku=sku)
    lines1 = ledger.lines_of(week=week1, city=city, channel=channel, sku=sku)
    c0, c1 = _cells(lines0), _cells(lines1)
    br = Bridge(label=label)
    br.comparable = sorted(set(c0) & set(c1))
    br.entered = sorted(set(c1) - set(c0))
    br.exited = sorted(set(c0) - set(c1))

    q0_total = sum(c0[k].units for k in br.comparable)
    q1_total = sum(c1[k].units for k in br.comparable)
    m0_bar = (Fraction(sum(c0[k].margin_ex for k in br.comparable)) / q0_total
              if q0_total else Fraction(0))

    for k in br.comparable:
        a, b = c0[k], c1[k]
        m0 = Fraction(a.margin_ex, a.units)
        m1 = Fraction(b.margin_ex, b.units)
        dq = b.units - a.units
        br.volume += dq * m0
        br.price += a.units * (Fraction(b.list_cents, b.units) - Fraction(a.list_cents, a.units))
        br.activity += -a.units * (Fraction(b.coupon_cents, b.units) - Fraction(a.coupon_cents, a.units))
        br.subsidy += a.units * (Fraction(b.subsidy_cents, b.units) - Fraction(a.subsidy_cents, a.units))
        br.cost += -a.units * (Fraction(b.cost_cents, b.units) - Fraction(a.cost_cents, a.units))
        br.interaction += dq * (m1 - m0)
        br.freight_change += -(b.freight_cents - a.freight_cents)
        br.refund_change += -(b.refund_net_cents - a.refund_net_cents)
    # 销量的其中项:纯量按基期份额×基期单位毛利;结构=销量-纯量(仅本切片口径)
    br.pure_volume = (q1_total - q0_total) * m0_bar
    br.mix = br.volume - br.pure_volume

    for k in br.entered:
        m = Fraction(c1[k].margin_ex)
        br.structural += m
        br.structural_detail.append((k, "新进", m))
        br.freight_change += -c1[k].freight_cents
        br.refund_change += -c1[k].refund_net_cents
    for k in br.exited:
        m = Fraction(c0[k].margin_ex)
        br.structural -= m
        br.structural_detail.append((k, "退出", -m))
        br.freight_change += c0[k].freight_cents
        br.refund_change += c0[k].refund_net_cents

    p0, p1 = week_parts(lines0), week_parts(lines1)
    br.delta_margin = Fraction(p1.margin - p0.margin)
    assert br.factor_sum() == br.delta_margin, f"{label}: 因子合计不等于毛利差"
    return br
