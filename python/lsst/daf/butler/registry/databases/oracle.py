# This file is part of daf_butler.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import annotations

__all__ = ["OracleDatabase"]

from contextlib import closing, contextmanager
import copy
from typing import Optional

import sqlalchemy
import sqlalchemy.ext.compiler

from ..interfaces import Database, ReadOnlyDatabaseError
from ...core import ddl
from ..nameShrinker import NameShrinker


class _Merge(sqlalchemy.sql.expression.Executable, sqlalchemy.sql.ClauseElement):
    """A SQLAlchemy query that compiles to a MERGE invocation that is the
    equivalent of PostgreSQL and SQLite's INSERT ... ON CONFLICT REPLACE on the
    primary key constraint for the table.
    """

    def __init__(self, table):
        super().__init__()
        self.table = table


@sqlalchemy.ext.compiler.compiles(_Merge, "oracle")
def _merge(merge, compiler, **kw):
    """Generate MERGE query for inserting or updating records.
    """
    table = merge.table
    preparer = compiler.preparer

    allColumns = [col.name for col in table.columns]
    pkColumns = [col.name for col in table.primary_key]
    nonPkColumns = [col for col in allColumns if col not in pkColumns]

    # To properly support type decorators defined in core/ddl.py we need
    # to pass column type to `bindparam`.
    selectColumns = [sqlalchemy.sql.bindparam(col.name, type_=col.type).label(col.name)
                     for col in table.columns]
    selectClause = sqlalchemy.sql.select(selectColumns)

    tableAlias = table.alias("t")
    tableAliasText = compiler.process(tableAlias, asfrom=True, **kw)
    selectAlias = selectClause.alias("d")
    selectAliasText = compiler.process(selectAlias, asfrom=True, **kw)

    condition = sqlalchemy.sql.and_(
        *[tableAlias.columns[col] == selectAlias.columns[col] for col in pkColumns]
    )
    conditionText = compiler.process(condition, **kw)

    query = f"MERGE INTO {tableAliasText}" \
            f"\nUSING {selectAliasText}" \
            f"\nON ({conditionText})"
    updates = []
    for col in nonPkColumns:
        src = compiler.process(selectAlias.columns[col], **kw)
        dst = compiler.process(tableAlias.columns[col], **kw)
        updates.append(f"{dst} = {src}")
    updates = ", ".join(updates)
    query += f"\nWHEN MATCHED THEN UPDATE SET {updates}"

    insertColumns = ", ".join([preparer.format_column(col) for col in table.columns])
    insertValues = ", ".join([compiler.process(selectAlias.columns[col], **kw) for col in allColumns])

    query += f"\nWHEN NOT MATCHED THEN INSERT ({insertColumns}) VALUES ({insertValues})"
    return query


class OracleDatabase(Database):
    """An implementation of the `Database` interface for Oracle.

    Parameters
    ----------
    connection : `sqlalchemy.engine.Connection`
        An existing connection created by a previous call to `connect`.
    origin : `int`
        An integer ID that should be used as the default for any datasets,
        quanta, or other entities that use a (autoincrement, origin) compound
        primary key.
    namespace : `str`, optional
        The namespace (schema) this database is associated with.  If `None`,
        the default schema for the connection is used (which may be `None`).
    writeable : `bool`, optional
        If `True`, allow write operations on the database, including
        ``CREATE TABLE``.
    prefix : `str`, optional
        Prefix to add to all table names, effectively defining a virtual
        schema that can coexist with others within the same actual database
        schema.  This prefix must not be used in the un-prefixed names of
        tables.

    Notes
    -----
    To use a prefix from standardized factory functions like `Database.fromUri`
    and `Database.fromConnectionStruct`, a '+' character in the namespace will
    be interpreted as a combination of ``namespace`` (first) and ``prefix``
    (second).  Either may be empty.  This does *not* work when constructing
    an `OracleDatabase` instance directly.
    """

    def __init__(self, *, connection: sqlalchemy.engine.Connection, origin: int,
                 namespace: Optional[str] = None, writeable: bool = True, prefix: Optional[str] = None):
        # Get the schema that was included/implicit in the URI we used to
        # connect.
        dbapi = connection.engine.raw_connection()
        namespace = dbapi.current_schema
        super().__init__(connection=connection, origin=origin, namespace=namespace)
        self._writeable = writeable
        self.dsn = dbapi.dsn
        self.prefix = prefix
        self._shrinker = NameShrinker(connection.engine.dialect.max_identifier_length)

    @classmethod
    def connect(cls, uri: str, *, writeable: bool = True) -> sqlalchemy.engine.Connection:
        connection = sqlalchemy.engine.create_engine(uri, pool_size=1).connect()
        # Work around SQLAlchemy assuming that the Oracle limit on identifier
        # names is even shorter than it is after 12.2.
        oracle_ver = connection.engine.dialect._get_server_version_info(connection)
        if oracle_ver < (12, 2):
            raise RuntimeError("Oracle server version >= 12.2 required.")
        connection.engine.dialect.max_identifier_length = 128
        return connection

    @classmethod
    def fromConnection(cls, connection: sqlalchemy.engine.Connection, *, origin: int,
                       namespace: Optional[str] = None, writeable: bool = True) -> Database:
        if namespace and "+" in namespace:
            namespace, prefix = namespace.split("+")
            if not namespace:
                namespace = None
            if not prefix:
                prefix = None
        else:
            prefix = None
        return cls(connection=connection, origin=origin, writeable=writeable, namespace=namespace,
                   prefix=prefix)

    @contextmanager
    def transaction(self, *, interrupting: bool = False) -> None:
        with super().transaction(interrupting=interrupting):
            if not self.isWriteable():
                with closing(self._connection.connection.cursor()) as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            yield

    def isWriteable(self) -> bool:
        return self._writeable

    def __str__(self) -> str:
        if self.namespace is None:
            name = self.dsn
        else:
            name = f"{self.dsn:self.namespace}"
        return f"Oracle@{name}"

    def shrinkDatabaseEntityName(self, original: str) -> str:
        return self._shrinker.shrink(original)

    def expandDatabaseEntityName(self, shrunk: str) -> str:
        return self._shrinker.expand(shrunk)

    def _convertForeignKeySpec(self, table: str, spec: ddl.ForeignKeySpec, metadata: sqlalchemy.MetaData,
                               **kwds) -> sqlalchemy.schema.ForeignKeyConstraint:
        if self.prefix is not None:
            spec = copy.copy(spec)
            spec.table = self.prefix + spec.table
        return super()._convertForeignKeySpec(table, spec, metadata, **kwds)

    def _convertTableSpec(self, name: str, spec: ddl.TableSpec, metadata: sqlalchemy.MetaData,
                          **kwds) -> sqlalchemy.schema.Table:
        if self.prefix is not None and not name.startswith(self.prefix):
            name = self.prefix + name
        return super()._convertTableSpec(name, spec, metadata, **kwds)

    def getExistingTable(self, name: str, spec: ddl.TableSpec) -> Optional[sqlalchemy.schema.Table]:
        if self.prefix is not None and not name.startswith(self.prefix):
            name = self.prefix + name
        return super().getExistingTable(name, spec)

    def replace(self, table: sqlalchemy.schema.Table, *rows: dict):
        if not self.isWriteable():
            raise ReadOnlyDatabaseError(f"Attempt to replace into read-only database '{self}'.")
        self._connection.execute(_Merge(table), *rows)

    prefix: Optional[str]
    """A prefix included in all table names to simulate a database namespace
    (`str` or `None`).
    """

    dsn: str
    """The TNS entry of the database this instance is connected to (`str`).
    """
