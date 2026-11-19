# 资料字典

CSV 为 UTF-8，第一行为列名。日期为北京时间，时间带偏移；资料可见时刻与业务发生时刻分别以字段含义为准。空值不能默认做零。

### `products.csv`

同品名不同规格的交易不能按名字合并。

字段：`sku`、`name`、`pack`。

### `stores.csv`

归属以销售生效期而非实际核销门店。

字段：`store`、`city`、`finance_contact`、`store_contact`、`valid_from`。

### `sales.csv`

总标价减券为客户成交额；外部补贴可确认为收入，不是自担活动费。订单号可跨行或渠道共享，销售归属取承诺店。

字段：`line`、`order`、`sold_day`、`attribution_store`、`redemption_store`、`channel`、`sku`、`units`、`list_cents`、`product_cost_cents`、`coupon_cents`、`outside_subsidy_cents`、`trip`。

### `freight.csv`

一次行程一份总费用，后到版本是全额。

字段：`trip`、`rev`、`known_at`、`cents`、`memo`。

### `billable.csv`

按销售行提供的计费复核重量。当整趟分母不全时不能把全部费用分到已知行。

字段：`line`、`rev`、`known_at`、`chargeable_kg`。

### `refunds.csv`

退款减少原销售周收入；回收商品冲减原成本。现金出流以实际付款时点为准。

字段：`refund`、`line`、`paid_at`、`cash_out_cents`、`recovered_product_cost_cents`。

### `recaps.csv`

旧复盘沟通原始登记；根因标识是旧版把问题交给谁时使用的解释，不是假定根因已成立。

字段：`recap`、`scope`、`through`、`cutoff`、`delivered_ack`、`assigned_root`、`owner`、`resolution`。

### `policy.json`

资料时区北京时间。异动参考线用于安排复盘，不改变财务金额。经营版按城投递，集团版只供财务，历史接收状态由本地表模拟。
