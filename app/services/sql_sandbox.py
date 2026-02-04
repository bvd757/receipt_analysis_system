from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class SQLSandboxError(ValueError):
    pass


_UID_TOKEN = "__bind_uid__"
_MAX_LIMIT = 200

_DANGEROUS_SUBSTRINGS = {
    "sqlite_master",
    "sqlite_temp_master",
    "sqlite_schema",
    "pragma",
    "attach",
    "detach",
    "vacuum",
    "load_extension",
    "reindex",
}


@dataclass(frozen=True)
class SandboxResult:
    sql: str
    params: dict


def sanitize_sql(sql: str, user_id: int) -> SandboxResult:
    """
    - only 1 statement
    - only SELECT (optionally WITH ... SELECT)
    - blocks dangerous keywords/tables
    - enforces receipts.user_id filter
    - enforces LIMIT 200
    """
    if not sql or not sql.strip():
        raise SQLSandboxError("Empty SQL")

    raw = sql.strip().strip(";").strip()
    raw_l = raw.lower()

    # quick reject: obvious dangerous substrings
    if any(s in raw_l for s in _DANGEROUS_SUBSTRINGS):
        raise SQLSandboxError("Dangerous SQL keyword/table detected")

    # reject multiple statements early
    if ";" in raw:
        raise SQLSandboxError("Multiple SQL statements are not allowed")

    try:
        parsed = sqlglot.parse(raw, read="sqlite")
    except Exception as e:
        raise SQLSandboxError(f"SQL parse error: {e}") from e

    if len(parsed) != 1:
        raise SQLSandboxError("Only one SQL statement is allowed")

    stmt = parsed[0]

    # must be SELECT (optionally WITH ... SELECT)
    outer = _outer_select(stmt)
    if outer is None:
        raise SQLSandboxError("Only SELECT statements are allowed")

    # walk the whole AST: forbid non-select DML/DDL
    _reject_forbidden_nodes(stmt)

    # allow only our tables + CTE names (so LLM can use WITH)
    allowed_tables = {"receipts", "receipt_items"} | _cte_names(stmt)
    _validate_tables(stmt, allowed_tables)

    # enforce tenant filter via receipts alias
    receipts_alias = _find_receipts_alias(stmt)
    if receipts_alias is None:
        raise SQLSandboxError("Query must reference 'receipts' table to enforce user scope")

    _enforce_user_filter(outer, receipts_alias)
    _enforce_limit(outer, _MAX_LIMIT)

    safe_sql = stmt.sql(dialect="sqlite").strip().rstrip(";")

    # replace our unique token with SQLAlchemy named bind param
    safe_sql = (
        safe_sql.replace(f'"{_UID_TOKEN}"', ":uid")
        .replace(f"`{_UID_TOKEN}`", ":uid")
        .replace(f"[{_UID_TOKEN}]", ":uid")
        .replace(_UID_TOKEN, ":uid")
    )

    return SandboxResult(sql=safe_sql, params={"uid": int(user_id)})


def _outer_select(stmt: exp.Expression) -> exp.Select | None:
    if isinstance(stmt, exp.Select):
        return stmt
    if isinstance(stmt, exp.With):
        return stmt.this if isinstance(stmt.this, exp.Select) else None
    return None


def _cte_names(stmt: exp.Expression) -> set[str]:
    names: set[str] = set()
    w = stmt if isinstance(stmt, exp.With) else stmt.find(exp.With)
    if not w:
        return names
    for cte in w.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(alias.lower())
    return names


def _reject_forbidden_nodes(stmt: exp.Expression) -> None:
    forbidden_names = [
        "Insert",
        "Update",
        "Delete",
        "Create",
        "Drop",
        "Alter",
        "Truncate",
        "Command",
        "Transaction",
    ]

    for name in forbidden_names:
        klass = getattr(exp, name, None)
        if klass is None:
            continue
        if next(stmt.find_all(klass), None) is not None:
            raise SQLSandboxError("Only SELECT is allowed (DML/DDL detected)")



def _validate_tables(stmt: exp.Expression, allowed_tables: set[str]) -> None:
    for t in stmt.find_all(exp.Table):
        name = (t.name or "").lower()
        if not name:
            continue
        if name in _DANGEROUS_SUBSTRINGS:
            raise SQLSandboxError("Access to system tables/pragma is not allowed")
        if name not in allowed_tables:
            raise SQLSandboxError(f"Table '{name}' is not allowed")


def _find_receipts_alias(stmt: exp.Expression) -> str | None:
    for t in stmt.find_all(exp.Table):
        if (t.name or "").lower() == "receipts":
            alias = t.alias_or_name
            return alias
    return None


def _enforce_user_filter(sel: exp.Select, receipts_alias: str) -> None:
    cond = exp.EQ(
        this=exp.column("user_id", table=receipts_alias),
        expression=exp.Identifier(this=_UID_TOKEN),
    )

    existing_where = sel.args.get("where")
    if existing_where is None:
        sel.set("where", exp.Where(this=cond))
    else:
        sel.set("where", exp.Where(this=exp.and_(existing_where.this, cond)))


def _enforce_limit(sel: exp.Select, limit: int) -> None:
    limit = int(limit)

    limit_node = exp.Limit()

    if "expression" in exp.Limit.arg_types:
        limit_node.set("expression", exp.Literal.number(limit))
    elif "this" in exp.Limit.arg_types:
        limit_node.set("this", exp.Literal.number(limit))
    else:
        tmp = sqlglot.parse_one(f"SELECT 1 LIMIT {limit}", read="sqlite")
        limit_node = tmp.args.get("limit")

    sel.set("limit", limit_node)
