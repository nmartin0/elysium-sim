"""Tests for reading a pack file.

The point of this layer is that a wrong pack is refused when it is
READ, so most of what follows is about what gets rejected and whether
the message tells the author where to look.
"""

import textwrap

import pytest

from simulator.lifecycle import Lifecycle
from simulator.schema import ColumnType
from simulator.spec import PackError, load_pack, load_spec

MINIMAL = {
    "pack": "shop",
    "silos": {"ops": {"kind": "postgresql", "database": "ops"}},
    "schemas": {"ops": {"tables": {"customers": {"columns": {
        "customer_id": {"type": "text", "length": 64,
                        "primary_key": True, "nullable": False},
        "name": {"type": "text", "length": 200, "nullable": False},
        "balance": {"type": "decimal", "precision": 19, "scale": 4},
    }}}}},
    "seed": [{
        "table": "ops.customers",
        "count": 5,
        "columns": {
            "customer_id": {"generator": "id", "prefix": "cust"},
            "name": {"generator": "choice", "options": ["Okafor", "Feldman"]},
        },
    }],
}


def with_change(**changes) -> dict:
    """A copy of the minimal pack with sections replaced."""
    return {**MINIMAL, **changes}


# -- a whole pack ----------------------------------------------------

def test_a_complete_pack_loads(tmp_path):
    source = textwrap.dedent("""
        pack: field_service
        description: A small plumbing firm.

        silos:
          dispatch: {kind: postgresql, database: dispatch}
          books:    {kind: rest}
          payroll:  {kind: filedrop}

        schemas:
          dispatch:
            tables:
              work_orders:
                columns:
                  work_order_id: {type: text, length: 64, primary_key: true, nullable: false}
                  status:        {type: text, length: 32, nullable: false}
                  quoted_total:  {type: decimal, precision: 19, scale: 4, nullable: false}

        curves:
          working_day: [0,0,0,0,0,0.1,0.4,0.9,1.2,1.3,1.2,1.0,
                        0.8,1.1,1.2,1.1,0.9,0.6,0.3,0.2,0.1,0.05,0,0]

        lifecycles:
          WorkOrder:
            initial: requested
            states:
              requested:
                - {to: quoted, per_hour: 0.4}
              quoted:
                - {to: approved, per_hour: 0.05, min_dwell: 4h}
              approved:
              cancelled:

        seed:
          - table: dispatch.work_orders
            count: 12
            columns:
              work_order_id: {generator: id, prefix: wo}
              status:        {generator: constant, value: requested}
              quoted_total:  {generator: decimal, min: 100, max: 4000, scale: 4}
        """)
    path = tmp_path / "field_service.yaml"
    path.write_text(source)

    pack = load_pack(path)
    assert pack.name == "field_service"
    # Three silo kinds in one business, which is the normal case rather
    # than an edge: a database, an API and a folder of files.
    assert {name: silo.kind for name, silo in pack.silos.items()} == {
        "dispatch": "postgresql", "books": "rest", "payroll": "filedrop"}
    assert pack.schemas["dispatch"].table("work_orders").column("quoted_total").type \
        is ColumnType.DECIMAL
    assert len(pack.curves["working_day"]) == 24
    assert isinstance(pack.lifecycles["WorkOrder"], Lifecycle)
    assert pack.seed[0].qualified == "dispatch.work_orders"
    assert pack.seed[0].count == 12


def test_durations_read_as_time_rather_than_seconds(tmp_path):
    # `min_dwell: 7d` against `min_dwell: 604800`.
    pack = load_spec(with_change(lifecycles={"Thing": {
        "initial": "a",
        "states": {
            "a": [{"to": "b", "per_hour": 1.0, "min_dwell": "4h"}],
            "b": [{"to": "c", "per_hour": 1.0, "min_dwell": "7d"}],
            "c": [{"to": "d", "per_hour": 1.0, "min_dwell": "30m"}],
            "d": None,
        },
    }}))
    states = pack.lifecycles["Thing"].states
    assert states["a"][0].min_dwell_seconds == 4 * 3600
    assert states["b"][0].min_dwell_seconds == 7 * 86400
    assert states["c"][0].min_dwell_seconds == 30 * 60


def test_a_state_with_nothing_under_it_is_terminal():
    pack = load_spec(with_change(lifecycles={"Thing": {
        "initial": "a", "states": {"a": [{"to": "done", "per_hour": 1.0}], "done": None},
    }}))
    assert pack.lifecycles["Thing"].states["done"] == []


# -- YAML's implicit typing ------------------------------------------

def test_null_as_a_key_is_explained_rather_than_baffling(tmp_path):
    # PyYAML follows YAML 1.1, where `null`, `on`, `off`, `yes` and `no`
    # are values rather than strings. `nullable: false` reads exactly
    # like DDL and is the obvious thing to write -- and produces a
    # mapping keyed by None, whose complaint names a key the author
    # cannot find anywhere in their file.
    path = tmp_path / "pack.yaml"
    path.write_text(textwrap.dedent("""
        pack: shop
        silos:
          ops: {kind: postgresql, database: ops}
        schemas:
          ops:
            tables:
              t:
                columns:
                  a: {type: text, null: false}
        """))
    with pytest.raises(PackError) as raised:
        load_pack(path)
    message = str(raised.value)
    assert "non-string key" in message
    assert "nullable" in message


@pytest.mark.parametrize("word", ["on", "off", "yes", "no"])
def test_the_other_reserved_words_are_caught_too(tmp_path, word):
    # Through real YAML, because that is the only place the trap
    # exists. A first version passed these as Python dict keys, where
    # they are ordinary strings and nothing goes wrong -- so it tested
    # nothing at all.
    path = tmp_path / "pack.yaml"
    path.write_text(textwrap.dedent(f"""
        pack: shop
        silos:
          ops: {{kind: postgresql, database: ops, {word}: 1}}
        """))
    with pytest.raises(PackError, match="non-string key"):
        load_pack(path)


# -- silos -----------------------------------------------------------

def test_a_pack_must_have_a_silo():
    with pytest.raises(PackError, match="at least one silo"):
        load_spec({"pack": "empty", "silos": {}})


def test_an_unknown_silo_kind_lists_the_real_ones():
    with pytest.raises(PackError, match="unknown silo kind 'oracle'"):
        load_spec(with_change(silos={"ops": {"kind": "oracle"}}))


# -- schemas ---------------------------------------------------------

def test_a_schema_for_an_undeclared_silo_is_refused():
    with pytest.raises(PackError, match="no silo called 'warehouse'"):
        load_spec(with_change(schemas={"warehouse": {"tables": {}}}))


def test_a_schema_on_a_non_relational_silo_is_refused():
    # A folder of CSV and a JSON API do not have tables. Allowing this
    # would produce a pack that looks complete and creates nothing.
    spec = with_change(
        silos={"drop": {"kind": "filedrop"}},
        schemas={"drop": {"tables": {"t": {"columns": {"a": {"type": "text"}}}}}},
        seed=[],
    )
    with pytest.raises(PackError, match="cannot hold tables"):
        load_spec(spec)


def test_a_relational_silo_holding_a_schema_must_name_a_database():
    spec = with_change(silos={"ops": {"kind": "postgresql"}}, seed=[])
    with pytest.raises(PackError, match="must name a database"):
        load_spec(spec)


def test_an_unknown_column_type_lists_the_real_ones():
    spec = with_change(schemas={"ops": {"tables": {"t": {"columns": {
        "a": {"type": "varchar"}}}}}}, seed=[])
    with pytest.raises(PackError, match="unknown column type 'varchar'"):
        load_spec(spec)


def test_a_decimal_without_precision_is_refused_with_its_path():
    spec = with_change(schemas={"ops": {"tables": {"t": {"columns": {
        "total": {"type": "decimal"}}}}}}, seed=[])
    with pytest.raises(PackError) as raised:
        load_spec(spec)
    # The path is the point: "precision is required" is useless in a
    # file with eighty columns.
    assert "schemas.ops.tables.t" in str(raised.value)
    assert "precision and scale" in str(raised.value)


def test_an_unrecognised_column_key_is_refused():
    spec = with_change(schemas={"ops": {"tables": {"t": {"columns": {
        "a": {"type": "text", "lenght": 5}}}}}}, seed=[])
    with pytest.raises(PackError, match="does not understand"):
        load_spec(spec)


# -- curves ----------------------------------------------------------

def test_a_curve_must_have_twenty_four_hours():
    with pytest.raises(PackError, match="24 hourly weights"):
        load_spec(with_change(curves={"day": [1.0] * 23}))


def test_a_curve_that_can_never_fire_is_refused():
    # Not a quiet domain -- a curve that produces no arrival ever, and
    # silently: the pack would simply do nothing.
    with pytest.raises(PackError, match="zero at every hour"):
        load_spec(with_change(curves={"day": [0.0] * 24}))


# -- lifecycles ------------------------------------------------------

def test_a_transition_to_an_undeclared_state_is_refused():
    # Caught where the declaration is, not hours into a run.
    with pytest.raises(PackError, match="not declared"):
        load_spec(with_change(lifecycles={"Thing": {
            "initial": "a", "states": {"a": [{"to": "nowhere", "per_hour": 1.0}]}}}))


def test_an_initial_state_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="initial state"):
        load_spec(with_change(lifecycles={"Thing": {
            "initial": "missing", "states": {"a": None}}}))


def test_a_transition_needs_a_rate():
    with pytest.raises(PackError, match="needs a per_hour rate"):
        load_spec(with_change(lifecycles={"Thing": {
            "initial": "a", "states": {"a": [{"to": "b"}], "b": None}}}))


def test_a_malformed_duration_says_what_one_looks_like():
    with pytest.raises(PackError, match="not a duration"):
        load_spec(with_change(lifecycles={"Thing": {
            "initial": "a",
            "states": {"a": [{"to": "b", "per_hour": 1.0, "min_dwell": "soon"}], "b": None}}}))


def test_an_unknown_duration_unit_lists_the_real_ones():
    with pytest.raises(PackError, match="unknown unit"):
        load_spec(with_change(lifecycles={"Thing": {
            "initial": "a",
            "states": {"a": [{"to": "b", "per_hour": 1.0, "min_dwell": "4y"}], "b": None}}}))


# -- seed ------------------------------------------------------------

def test_a_seed_table_must_be_qualified():
    with pytest.raises(PackError, match="written as silo.table"):
        load_spec(with_change(seed=[{"table": "customers", "count": 1,
                                     "columns": {"customer_id": {"generator": "id",
                                                                 "prefix": "c"}}}]))


def test_a_seed_table_that_does_not_exist_is_refused():
    with pytest.raises(PackError, match="has no table 'widgets'"):
        load_spec(with_change(seed=[{"table": "ops.widgets", "count": 1,
                                     "columns": {"a": {"generator": "now"}}}]))


def test_a_seed_column_that_the_table_does_not_have_is_refused():
    step = dict(MINIMAL["seed"][0])
    step["columns"] = {**step["columns"], "loyalty_tier": {"generator": "now"}}
    with pytest.raises(PackError, match=r"no column\(s\) \['loyalty_tier'\]"):
        load_spec(with_change(seed=[step]))


def test_every_non_null_column_must_be_generated():
    # A pack omitting one produces rows the engine rejects, which
    # surfaces as a database error mid-seed rather than a pack problem.
    step = dict(MINIMAL["seed"][0])
    step["columns"] = {"customer_id": {"generator": "id", "prefix": "c"}}
    with pytest.raises(PackError, match=r"non-null column\(s\) \['name'\]"):
        load_spec(with_change(seed=[step]))


def test_a_nullable_column_may_be_left_out():
    # `balance` is nullable and ungenerated in the minimal pack.
    assert load_spec(MINIMAL).seed[0].count == 5


def test_a_seed_generator_cannot_refer_to_a_subject():
    # THE check that justifies generators.references(). A seed step has
    # no subject and nothing picked or emitted, so this is a pack error
    # that is detectable without running anything.
    step = dict(MINIMAL["seed"][0])
    step["columns"] = {**step["columns"],
                       "balance": {"generator": "reference", "from": "subject.limit"}}
    with pytest.raises(PackError, match="cannot refer to 'subject.limit' while seeding"):
        load_spec(with_change(seed=[step]))


def test_a_seed_expression_referring_to_a_missing_column_is_refused():
    step = dict(MINIMAL["seed"][0])
    step["columns"] = {**step["columns"],
                       "balance": {"generator": "expression", "expression": "markup * 2"}}
    with pytest.raises(PackError, match="refers to 'markup'"):
        load_spec(with_change(seed=[step]))


def test_a_seed_expression_over_declared_columns_is_accepted():
    step = {
        "table": "ops.customers", "count": 2,
        "columns": {
            "customer_id": {"generator": "id", "prefix": "c"},
            "name": {"generator": "constant", "value": "X"},
            "balance": {"generator": "expression", "expression": "1 + 2"},
        },
    }
    assert load_spec(with_change(seed=[step])).seed[0].count == 2


def test_a_bad_generator_inside_a_seed_carries_the_column_path():
    step = dict(MINIMAL["seed"][0])
    step["columns"] = {**step["columns"], "balance": {"generator": "integer", "min": 1}}
    with pytest.raises(PackError) as raised:
        load_spec(with_change(seed=[step]))
    assert "seed[0].columns.balance" in str(raised.value)


def test_a_seed_count_must_be_positive():
    step = dict(MINIMAL["seed"][0])
    step["count"] = 0
    with pytest.raises(PackError, match="positive whole number"):
        load_spec(with_change(seed=[step]))


# -- file level ------------------------------------------------------

def test_invalid_yaml_says_so(tmp_path):
    path = tmp_path / "pack.yaml"
    path.write_text("pack: shop\n  bad indent: true\n")
    with pytest.raises(PackError, match="not valid YAML"):
        load_pack(path)


def test_a_non_mapping_pack_is_refused(tmp_path):
    path = tmp_path / "pack.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(PackError, match="mapping at the top level"):
        load_pack(path)


def test_a_pack_must_be_named():
    with pytest.raises(PackError, match="non-empty string"):
        load_spec({"silos": {"ops": {"kind": "postgresql"}}})
