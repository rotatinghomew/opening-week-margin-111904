"""原始资料装载与口径常量。

口径约定(与 DATA.md 一致,在此固化为代码):
- 客户成交额 = 总标价 - 券;券是自担活动费,外部补贴是可确认收入,二者不同性质。
- 归属取承诺店(attribution_store),不按实际核销门店;门店须已过 valid_from。
- 同品名不同规格不并名:规格口径就是 sku。
- 运费一次行程一份总费用,后到版本是全额(不是差额)。
- 计费重量以业务登记(billable)的已知时刻为准;空值不能默认做零。
- 退款减少原销售周收入、回收冲减原成本;现金流出落在实际付款所在的现金周。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

STORE_CITY = {"A": "西城", "B": "北城", "C": "南城"}
CITIES = ["西城", "北城", "南城"]
CHANNELS = ["线上", "柜台"]
SKUS = ["S", "L", "M"]


@dataclass(frozen=True)
class Week:
    name: str
    first: str
    last: str

    def contains(self, day: str) -> bool:
        return self.first <= day <= self.last


@dataclass(frozen=True)
class Policy:
    weeks: tuple[Week, ...]
    cuts: tuple[str, ...]
    weekly_local_time: str
    look_at_cents: int
    contacts: dict[str, str]

    def week_of(self, day: str) -> str | None:
        """销售观察周;现金周按同一 7 天节奏向后延伸(W3...)。"""
        for w in self.weeks:
            if w.contains(day):
                return w.name
        base = self.weeks[0].first
        delta = (int(day[8:10]) + _month_offset(day)) - (int(base[8:10]) + _month_offset(base))
        if delta >= 0:
            return f"W{delta // 7 + 1}"
        return None

    def week_span(self, name: str) -> tuple[str, str]:
        for w in self.weeks:
            if w.name == name:
                return w.first, w.last
        # 延伸现金周
        idx = int(name[1:]) - 1
        y, m, d = _shift_weeks(self.weeks[0].first, idx)
        first = f"{y:04d}-{m:02d}-{d:02d}"
        y, m, d = _shift_weeks(self.weeks[0].last, idx)
        return first, f"{y:04d}-{m:02d}-{d:02d}"


def _month_offset(day: str) -> int:
    return int(day[5:7]) * 31


def _shift_weeks(day: str, weeks: int) -> tuple[int, int, int]:
    import datetime as _dt

    d = _dt.date.fromisoformat(day) + _dt.timedelta(weeks=weeks)
    return d.year, d.month, d.day


@dataclass(frozen=True)
class SaleLine:
    line: str
    order: str
    sold_day: str
    store: str
    city: str
    redemption_store: str
    channel: str
    sku: str
    units: int
    list_cents: int
    product_cost_cents: int
    coupon_cents: int
    outside_subsidy_cents: int
    trip: str


@dataclass(frozen=True)
class FreightRev:
    trip: str
    rev: int
    known_at: str
    cents: int
    memo: str


@dataclass(frozen=True)
class Billable:
    line: str
    rev: int
    known_at: str
    chargeable_kg: int


@dataclass(frozen=True)
class Refund:
    refund: str
    line: str
    paid_at: str
    cash_out_cents: int
    recovered_product_cost_cents: int


@dataclass(frozen=True)
class Recap:
    recap: str
    scope: str
    through: str
    cutoff: str
    delivered_ack: str
    assigned_root: str
    owner: str
    resolution: str


@dataclass(frozen=True)
class Source:
    policy: Policy
    sales: tuple[SaleLine, ...]
    freight: tuple[FreightRev, ...]
    billable: tuple[Billable, ...]
    refunds: tuple[Refund, ...]
    recaps: tuple[Recap, ...]
    store_valid_from: dict[str, str]
    store_contact: dict[str, str]
    sku_name: dict[str, str]  # sku -> "品名/规格",仅展示用;比较口径仍按 sku,不并名


def _rows(name: str) -> list[dict[str, str]]:
    with (DATA / f"{name}.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_source(root: Path | None = None) -> Source:
    global DATA
    if root is not None:
        DATA = root / "data"
    raw_policy = json.loads((DATA / "policy.json").read_text(encoding="utf-8"))
    policy = Policy(
        weeks=tuple(Week(f"W{i + 1}", w["first"], w["last"]) for i, w in enumerate(raw_policy["weeks"])),
        cuts=tuple(raw_policy["cuts"]),
        weekly_local_time=raw_policy["weekly_local_time"],
        look_at_cents=int(raw_policy["look_at_cents"]),
        contacts=dict(raw_policy["contacts"]),
    )
    valid_from = {r["store"]: r["valid_from"] for r in _rows("stores")}
    contact = {r["store"]: r["store_contact"] for r in _rows("stores")}
    sku_name = {r["sku"]: f"{r['name']}·{r['pack']}" for r in _rows("products")}
    sales = tuple(
        SaleLine(
            line=r["line"], order=r["order"], sold_day=r["sold_day"],
            store=r["attribution_store"], city=STORE_CITY[r["attribution_store"]],
            redemption_store=r["redemption_store"], channel=r["channel"], sku=r["sku"],
            units=int(r["units"]), list_cents=int(r["list_cents"]),
            product_cost_cents=int(r["product_cost_cents"]), coupon_cents=int(r["coupon_cents"]),
            outside_subsidy_cents=int(r["outside_subsidy_cents"]), trip=r["trip"],
        )
        for r in _rows("sales")
    )
    freight = tuple(
        FreightRev(trip=r["trip"], rev=int(r["rev"]), known_at=r["known_at"],
                   cents=int(r["cents"]), memo=r["memo"])
        for r in _rows("freight")
    )
    billable = tuple(
        Billable(line=r["line"], rev=int(r["rev"]), known_at=r["known_at"],
                 chargeable_kg=int(r["chargeable_kg"]))
        for r in _rows("billable")
    )
    refunds = tuple(
        Refund(refund=r["refund"], line=r["line"], paid_at=r["paid_at"],
               cash_out_cents=int(r["cash_out_cents"]),
               recovered_product_cost_cents=int(r["recovered_product_cost_cents"]))
        for r in _rows("refunds")
    )
    recaps = tuple(
        Recap(recap=r["recap"], scope=r["scope"], through=r["through"], cutoff=r["cutoff"],
              delivered_ack=r["delivered_ack"], assigned_root=r["assigned_root"],
              owner=r["owner"], resolution=r["resolution"])
        for r in _rows("recaps")
    )
    return Source(policy=policy, sales=sales, freight=freight, billable=billable,
                  refunds=refunds, recaps=recaps, store_valid_from=valid_from,
                  store_contact=contact, sku_name=sku_name)
