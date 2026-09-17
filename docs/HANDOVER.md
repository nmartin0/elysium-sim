# Ashgrove Plumbing & Heating — systems handover

Prepared for the engineer installing Elysium.

We are a plumbing and heating firm in Bristol with three branches and
four engineers on the road. This is everything we have, where it is,
and how to get at it. We have no IT department — our systems were set
up by whoever was available at the time — so some of what follows is
us telling you what we have noticed rather than what we designed.

**What we are not giving you** is any view on how it should be
modelled. We do not know what an ontology is. What a customer or a job
*means* for your purposes is your call, and we would rather you
decided that after you have looked at the data than after we have
described it badly.

---

## 1. What we run

Three systems. They do not talk to each other.

| | what it is | what it holds |
| --- | --- | --- |
| **Dispatch** | PostgreSQL 16 | Customers, engineers, jobs, invoices, pay lines. The business. |
| **Books** | A small HTTP service | What our bookkeeper sees. Invoices only, and fewer fields than Dispatch has. |
| **Payroll** | A folder of CSV files | A weekly export of what each engineer earned. |

Dispatch is the one that matters. Books and Payroll are both fed *from*
Dispatch, on a schedule, and neither feeds anything back.

---

## 2. Getting in

When the simulator is running it writes a file called
`connections.json` into the world directory. That file is the truth
about where things are — **the ports change every run**, so do not
write them down.

```json
{
  "pack": "field_service",
  "silos": {
    "dispatch": {
      "kind": "postgresql",
      "host": "127.0.0.1",
      "port": 45253,
      "database": "dispatch",
      "user": "reader",
      "writer_user": "writer"
    },
    "books": {
      "kind": "rest",
      "base_url": "http://127.0.0.1:35933",
      "format": "json",
      "auth": "bearer",
      "token": "sim-token"
    },
    "payroll": {
      "kind": "filedrop",
      "path": "<world>/payroll/payroll",
      "format": "csv",
      "encoding": "utf-8-sig"
    }
  }
}
```

### 2.1 Dispatch

Standard PostgreSQL. No password — it only listens on `127.0.0.1`, and
our old IT contractor said that was fine.

**We have given you two accounts.**

- `reader` — can `SELECT`, and nothing else. Use this for anything
  that reads.
- `writer` — can `SELECT`, `INSERT` and `UPDATE`. Use this only if
  your tool writes back.

Neither can `DELETE`, `DROP`, `TRUNCATE` or `ALTER` anything. That is
deliberate and it is not negotiable — we have been burned before. If
your tool needs to do any of those, come and talk to us rather than
asking for a bigger account.

One thing that surprised our contractor and may surprise you:
`information_schema.table_constraints` comes back **empty** for both
accounts. That is normal for an account with only `SELECT`. If you are
looking for primary keys, `information_schema.key_column_usage` works
fine.

### 2.2 Books

HTTP, JSON, bearer token in the `Authorization` header.

```
GET /v1/invoices
Authorization: Bearer sim-token
```

It returns `{"data": [...], "cursor": "..."}` and **25 records at a
time**. If there is a `cursor`, there is more — pass it back as
`?cursor=`. We mention this because the last person to read it took
the first page and told us our turnover was a tenth of what it is.

Money comes back as a **string**, not a number. Our bookkeeper insisted.

An unknown collection gives you a 404 and a missing token gives you a
401, both as JSON. Neither means the service is down.

### 2.3 Payroll

A folder. One CSV per week, named `pay_lines_YYYY-MM-DD.csv` after the
week it covers, so they sort in order. A file appears complete or not
at all — it is written under a temporary name and renamed.

**The files have a byte-order mark**, because they get opened in Excel
and without one the accents come out wrong. If you read them as plain
UTF-8 your first column name will have three stray characters on the
front. Read them as `utf-8-sig`.

---

## 3. What is in Dispatch

Five tables. Row counts below are from a 45-day run and will differ on
yours.

### customers (30 rows)

| column | type | |
| --- | --- | --- |
| `customer_id` | text | primary key |
| `name` | text | not null |
| `phone` | text | |
| `balance_owed` | decimal(19,4) | not null |
| `joined_on` | date | not null |
| `created_at` | timestamp | not null |
| `updated_at` | timestamp | not null |
| `created_by` | text | not null |
| `branch` | text | not null |
| `account_type` | text | not null |

`branch` is which of our three branches looks after them — `bristol`,
`bath` or `weston`. We use it for almost everything: who gets the call,
whose numbers a branch manager is allowed to see.

`account_type` is `trade` or `domestic`. Trade accounts only exist at
Bristol; that is not a rule anybody wrote down, it is just how it
happened.

`created_by` is whoever put the record in — `reception`, `import` or
`engineer`. The `import` ones came from the old system.

`updated_at` moves whenever the record changes, which in practice means
whenever their balance does.

### work_orders (371 rows)

| column | type | |
| --- | --- | --- |
| `work_order_id` | text | primary key |
| `customer_id` | text | not null |
| `status` | text | not null |
| `raised_at` | timestamp | not null |
| `quoted_total` | decimal(19,4) | |
| `quoted_at` | timestamp | |
| `completed_at` | timestamp | |
| `notes` | text | |

A job. `status` moves through `requested` → `quoted` → `approved` →
`completed` → `invoiced` → `paid`, and can go to `cancelled` early on
or to `disputed` once we have billed for it.

You will also find `archived`, which is **not** part of that sequence —
see section 4.

`notes` is what the engineer wrote up afterwards. Only jobs that got
finished have them.

A work order has no branch of its own. It belongs to the customer, and
the customer has the branch.

### invoices (331 rows)

| column | type | |
| --- | --- | --- |
| `invoice_id` | text | primary key |
| `work_order_id` | text | not null |
| `customer_id` | text | not null |
| `total` | decimal(19,4) | not null |
| `issued_at` | timestamp | not null |
| `paid` | boolean | not null |
| `is_void` | boolean | not null |
| `voided_at` | timestamp | |

### pay_lines (333 rows)

| column | type | |
| --- | --- | --- |
| `pay_line_id` | text | primary key |
| `technician_id` | text | not null |
| `work_order_id` | text | not null |
| `amount` | decimal(19,4) | not null |
| `earned_on` | date | not null |

What an engineer earned on a job. This is what gets exported to
Payroll.

### technicians (4 rows)

| column | type | |
| --- | --- | --- |
| `technician_id` | text | primary key |
| `name` | text | not null |
| `hourly_rate` | decimal(19,4) | not null |

Our four engineers.

### skills (6 rows) and technician_skills (8 rows)

What each engineer is qualified for. `skills` is the list;
`technician_skills` pairs them up.

| column | type | |
| --- | --- | --- |
| `technician_skill_id` | text | primary key |
| `technician_id` | text | not null |
| `skill_id` | text | not null |

Two rows per engineer. **The pair itself is not unique** — the key is
on `technician_skill_id`, not on the two together, so the same engineer
can be recorded twice for the same skill and sometimes is. If you count
skills per engineer without a `DISTINCT` you will get the wrong answer.

---

## 4. Things we know are wrong with it

We would rather tell you now than have you find them.

**Six jobs have no customer.** When we came off the old system in
January, some jobs came across and their customer records did not. They
are the ones with `status = 'archived'` and a `customer_id` beginning
`cust_gone_`. Their notes say where they came from. We cannot tell you
who those jobs were for — nobody can, the records are gone. They are
about 2% of the table, so it is easy not to notice them.

**Voided invoices are still there.** When a customer disputes a bill we
mark it `is_void` rather than deleting it, because we have to be able
to show what happened — the job goes to `disputed` and its invoice is
voided. About 3-4% of invoices end up voided, fairly evenly across the
year. **If you total
the `total` column without filtering `is_void` you will overstate our
revenue by roughly 5%**, which is close enough to right that you will
not spot it. The last person to look at our numbers did exactly this.

**Books holds less than Dispatch does.** The `/v1/invoices` feed gives
you `invoice_id`, `work_order_id`, `total`, `issued_at` and `paid` —
and that is all. There is **no `customer_id`** and **no `is_void`**. So
you cannot tell from Books alone who an invoice was for or whether it
still counts. If you need either, you have to go to Dispatch. We have
asked the bookkeeper's supplier about it twice.

**`status = 'archived'` is not one of our statuses.** It came in with
the migration and stuck. If you build anything that assumes the six
statuses in section 3 are the whole list, it will be wrong about those
jobs.

**The skills table repeats itself.** See above — `technician_skills`
has no constraint on the pair, so duplicates get in. We have never
tidied them up.

**Nothing enforces the links.** There are no foreign keys anywhere.
`work_orders.customer_id` usually points at a customer, `invoices`
usually points at a job, but the database is not checking and — see
above — it is not always true.

---

## 5. What we want out of it

Not a specification, just so you know what we are after:

- Who owes us money, and how long it has been.
- What each branch is doing, without Bath seeing Bristol's numbers.
- What our engineers are earning and whether the payroll export
  matches what Dispatch thinks.
- Somebody to be able to ask "what happened on this job" and get the
  engineer's write-up back rather than a row of identifiers.

How you get there is up to you.

---

## 6. Before you blame Elysium

If something is not working, check the systems themselves first. Run:

```
simulator verify --dir <world>
```

It connects to all three the way your tool would — using only
`connections.json` — and tells you whether they are sound. If it
reports a problem, the fault is on our side and you should not spend
the afternoon in Elysium's logs.

It checks that each system is reachable, that the tables are there and
have rows, that every table has a primary key, that `reader` genuinely
cannot write, that the CSV folder has complete files with the byte-order
mark, and that the API pages properly.

---

*Anything else, ask for Dawn on reception. She has been here longest
and knows why most of it is the way it is.*
