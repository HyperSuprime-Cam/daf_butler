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

__all__ = ("SqliteRegistry", )

from contextlib import closing

from sqlalchemy import event
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from sqlite3 import Connection as SQLite3Connection

from lsst.daf.butler.core.config import Config
from lsst.daf.butler.core.registry import RegistryConfig
from lsst.daf.butler.core.repoRelocation import replaceRoot

from .sqlRegistry import SqlRegistry, SqlRegistryConfig


def _onSqlite3Connect(dbapiConnection, connectionRecord):
    assert isinstance(dbapiConnection, SQLite3Connection)
    # Prevent pysqlite from emitting BEGIN and COMMIT statements.
    dbapiConnection.isolation_level = None
    # Enable foreign keys
    with closing(dbapiConnection.cursor()) as cursor:
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA busy_timeout = 300000;")  # in ms, so 5min (way longer than should be needed)


def _onSqlite3Begin(connection):
    assert connection.dialect.name == "sqlite"
    # Replace pysqlite's buggy transaction handling that never BEGINs with our
    # own that does, and tell SQLite not to try to acquire a lock as soon as
    # we start a transaction (this should lead to more blocking and fewer
    # deadlocks).
    connection.execute("BEGIN IMMEDIATE")
    return connection


class SqliteRegistry(SqlRegistry):
    """Registry backed by a SQLite database.

    Parameters
    ----------
    config : `SqlRegistryConfig` or `str`
        Load configuration
    """

    @classmethod
    def setConfigRoot(cls, root, config, full):
        """Set any filesystem-dependent config options for this Registry to
        be appropriate for a new empty repository with the given root.

        Parameters
        ----------
        root : `str`
            Filesystem path to the root of the data repository.
        config : `Config`
            A Butler-level config object to update (but not a
            `ButlerConfig`, to avoid included expanded defaults).
        full : `ButlerConfig`
            A complete Butler config with all defaults expanded;
            repository-specific options that should not be obtained
            from defaults when Butler instances are constructed
            should be copied from `full` to `Config`.
        """
        super().setConfigRoot(root, config, full)
        Config.overrideParameters(RegistryConfig, config, full,
                                  toUpdate={".db.url": f"sqlite:///{root}/gen3.sqlite3"},
                                  toCopy=("cls", "deferDatasetIdQueries"))

    def __init__(self, registryConfig, schemaConfig, dimensionConfig, create=False, butlerRoot=None):
        registryConfig = SqlRegistryConfig(registryConfig)
        if ".db.url" in registryConfig:
            registryConfig[".db.url"] = replaceRoot(registryConfig[".db.url"], butlerRoot)
        if ":memory:" in registryConfig.get(".db.url", ""):
            create = True
        super().__init__(registryConfig, schemaConfig, dimensionConfig, create, butlerRoot=butlerRoot)

    def _createEngine(self):
        engine = create_engine(self.config[".db.url"], poolclass=NullPool,
                               connect_args={"check_same_thread": False})
        event.listen(engine, "connect", _onSqlite3Connect)
        event.listen(engine, "begin", _onSqlite3Begin)
        return engine
