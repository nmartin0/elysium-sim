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

**Neither account can read `pay_lines`.** What the engineers earn is
not everybody's business, and that is the one table we hold back. You
will get a permission error, not an empty result — if your tool
reports it as a connection problem, that is your tool being wrong
about what happened.

Worth knowing which catalogue you are reading: `pay_lines` does not
appear in `information_schema.tables` for these accounts, because
PostgreSQL filters that by privilege. It **does** appear in
`pg_tables`, which is not filtered. So depending on where you look,
you either see a table you cannot read or you do not know it exists.
Neither view tells you the other one differs.

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
week it covers, so they sort in order. **Each file holds that week
only** — if you want the year you have to read all of them. A file appears complete or not
at all — it is written under a temporary name and renamed.

**The files have a byte-order mark**, because they get opened in Excel
and without one the accents come out wrong. If you read them as plain
UTF-8 your first column name will have three stray characters on the
front. Read them as `utf-8-sig`.

---

## 3. What is in Dispatch

Five tables. Row counts below are from a 45-day run and will differ on
yours.

### customers (33 rows)

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
| `status_since` | timestamp | |

A job. `status` moves through `requested` → `quoted` → `approved` →
`completed` → `invoiced` → `paid`, and can go to `cancelled` early on
or to `disputed` once we have billed for it.

You will also find `archived`, which is **not** part of that sequence —
see section 4.

`notes` is what the engineer wrote up afterwards. Only jobs that got
finished have them.

`status_since` is when the job reached the status it is in. "How long
has this been sitting at quoted" is the question we ask most, and it
is the one thing the old system could never answer. The six migrated
jobs have nothing in it.

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

### pay_lines (333 rows) — withheld

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

Two rows per engineer. **Nothing in the database stops a duplicate** —
the key is on `technician_skill_id`, not on the two together, so
`(engineer, skill)` could be written twice and the database would
accept it. As far as we know it has not happened. Take that as "we
have been careful" rather than as a guarantee, because there is no
constraint enforcing it.

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

**Books holds less than Dispatch does, in two ways.** The
`/v1/invoices` feed gives you `invoice_id`, `work_order_id`, `total`,
`issued_at` and `paid` — and that is all. There is **no `customer_id`**
and **no `is_void`**. So you cannot tell from Books alone who an
invoice was for or whether it still counts.

It also only keeps **the last three months**. Anything older has been
archived out of it. If you total what Books gives you and call it our
turnover, you will report a quarter of the year. Both of these mean
Dispatch is the only place with the whole picture. We have asked the
bookkeeper's supplier about it twice.

**`status = 'archived'` is not one of our statuses.** It came in with
the migration and stuck. If you build anything that assumes the six
statuses in section 3 are the whole list, it will be wrong about those
jobs.

**Some households are in there twice.** Reception takes a call, cannot
find the customer, and makes a new record. Three households have two
records each — same name, same phone number, different
`customer_id` — and **both records have jobs and bills against them**,
because whichever one was found on the day is the one the work went
against. Neither record is the whole story for that household. We know
about it and we have never had time to merge them. Nothing in the
database says the two are the same people; you would have to decide
that from the name and the number, and it is your call whether that is
safe.

**The payroll files and Dispatch disagree, and both are right.** When a
customer disputes a bill we take the engineer's commission back off
that job — but a dispute usually lands a week or more after we
invoiced, and payroll goes out every Friday. So the CSV for that week
has already been written and sent with the original figure, and
Dispatch now shows nought.

Neither is wrong. The file is what was true when it was produced and
what we actually paid; Dispatch is what we think was earned. Over the
last ten weeks that is about 18 lines out of 579, and we are roughly
£4,500 up on what the files say we paid out. We have never
reconciled it. If you need "what did we pay" the files are the answer;
if you need "what was earned" Dispatch is.

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

And if the simulator was killed rather than stopped — you closed the
terminal, or the machine ran out of memory — the databases it started
carry on running with nothing left that knows about them, holding
their ports. That is what this is for:

```
simulator clean --dir <world>
```

It stops them and leaves the data alone, so you can still pick the
world up where it was. Add `--remove` when you are finished with it
and want the disk back.

---

*Anything else, ask for Dawn on reception. She has been here longest
and knows why most of it is the way it is.*
