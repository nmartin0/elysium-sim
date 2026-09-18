# Marchwood Homeware — systems handover

Prepared for the engineer installing Elysium.

We are an independent homeware shop. One shop on the high street, a
website, and two of us who understand any of it. This is what we have
and how to get at it.

Like the plumbing firm's pack, **we have no view on how any of this
should be modelled.** What a customer or an order *means* for your
purposes is your call.

---

## 1. What we run

| | what it is | what it holds |
| --- | --- | --- |
| **Web** | MariaDB | The website. Customers, the catalogue, orders and their lines. |
| **Till** | PostgreSQL | The till in the shop. Every transaction rung through the counter. |
| **Supplier** | A folder of CSV files | What we send our supplier each week. |

**The Web database and the Till do not talk to each other.** The till
is a separate machine from a different vendor. It was supposed to sync
nightly and it has never worked properly, so in practice the two only
agree by coincidence. Section 4 is mostly about this.

---

## 2. Getting in

`connections.json` in the world directory is the truth about where
things are. **The ports change every run.**

### 2.1 Web (MariaDB)

Standard MariaDB. Two accounts, same as anywhere: `reader` for
`SELECT` only, `writer` for `SELECT`, `INSERT` and `UPDATE`. Neither
can `DELETE`, `DROP`, `TRUNCATE` or `ALTER`.

`information_schema.table_constraints` comes back empty for both, which
is normal for an account with only `SELECT`. Use
`key_column_usage` for primary keys.

### 2.2 Till (PostgreSQL)

Same two accounts. The only table is `transactions`.

**It is append-only.** Nothing enforces that — there is no constraint,
the `writer` account could update a row — but nothing ever has. A
mistake at the till becomes a second transaction that reverses the
first, because that is how the till software works. If you see an
updated transaction, something is wrong.

### 2.3 Supplier

A folder. One CSV per week, `sold_YYYY-MM-DD.csv`, holding **that
week's counter sales only**. Written under a temporary name and
renamed, so a file is complete or absent.

Byte-order mark, because the supplier opens them in Excel. Read them
as `utf-8-sig`.

---

## 3. What is in them

### Web

**customers** — `customer_id`, `email`, `name`, `segment`,
`joined_on`, `created_at`, `updated_at`.

`segment` is which list somebody is on: `retail`, `trade` or `staff`.
We use it for pricing decisions and for who is allowed to see what.

**products** — `sku`, `name`, `unit_price`, `stock_on_hand`,
`discontinued`.

**orders** — `order_id`, `customer_id`, `status`, `placed_at`,
`goods_total`, `delivery`, `dispatched_at`, `notes`.

`status` runs `placed` → `picked` → `dispatched` → `delivered`, and can
go to `cancelled` early or `returned` after it has arrived.

`delivery` follows our rule: **free over £50, £2.99 from £25, £4.99
below that.** If you find an order that breaks it, tell us, because
that would be a bug in the website.

**order_lines** — `order_line_id`, `order_id`, `sku`, `description`,
`unit_price`, `quantity`.

The `description` and `unit_price` are **copied from the product at
the time of the order**, not looked up. That is deliberate on the
website's part — a price change should not rewrite history — so an old
line can disagree with the current catalogue and both are right.

### Till

**transactions** — `transaction_id`, `sku`, `quantity`, `taken`,
`rang_at`, `register`. Two registers, `till_1` and `till_2`.

---

## 4. Things we know are wrong with it

**The website does not know about counter sales.** Somebody buys a
lamp in the shop; the till records it and `stock_on_hand` on the
website does not move. So the website's stock figures are wrong by
however much we have sold over the counter, and they only get less
wrong when somebody counts the shelves. If you build anything that
trusts `stock_on_hand`, it will be confidently wrong.

**Returned orders keep their totals.** When something comes back we
change the status to `returned` and leave `goods_total` alone, because
the order really was placed for that amount. **If you total
`goods_total` without excluding `returned` you will overstate our
revenue**, by something like 7%. Nothing in the data warns you.

**The two systems share sku and nothing else.** A till transaction
names a product from the same catalogue, and that is the only join
between the two databases. There is no foreign key, no constraint, and
nothing checks it. It works because both were told the same list.

**Nothing enforces any of the links.** `orders.customer_id` usually
points at a customer, `order_lines.order_id` usually points at an
order. The database is not checking.

---

## 5. What we want out of it

- What is actually selling, counting both the website and the counter.
- Which customers are worth talking to, without the trade list seeing
  retail prices or the other way round.
- Whether our stock figures can be trusted, and where they go wrong.
- Somebody able to ask "what happened with this order" and get the
  note rather than a row of identifiers.

---

## 6. Before you blame Elysium

```
simulator verify --dir <world>
```

Connects to all three the way your tool would, using only
`connections.json`, and says whether they are sound. If it reports a
problem, the fault is ours.
