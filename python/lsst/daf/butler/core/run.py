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

from .utils import slotValuesAreEqual, slotValuesToHash

__all__ = ("Run", )


class Run(object):
    """Represent a processing run.

    Parameters
    ----------
    runId : `int`
        ID to associate with this run.
    registryId : `int`
        ID associated with this `Registry`.
    collection : `str`
        Collection to use for this run.
    environment : `str`
        Something about the environment.
    pipeline : `str`
        Something about the pipeline.
    """
    _currentId = 0

    @classmethod
    def getNewId(cls):
        cls._currentId += 1
        return cls._currentId

    __slots__ = ("_runId", "_registryId", "_collection", "_environment", "_pipeline")
    __eq__ = slotValuesAreEqual
    __hash__ = slotValuesToHash

    def __init__(self, runId, registryId, collection, environment, pipeline):
        self._runId = runId
        self._registryId = registryId
        self._collection = collection
        self._environment = environment
        self._pipeline = pipeline

    @property
    def pkey(self):
        return (self._runId, self.registryId)

    @property
    def runId(self):
        return self._runId

    @property
    def registryId(self):
        return self._registryId

    @property
    def collection(self):
        return self._collection

    @property
    def environment(self):
        return self._environment

    @property
    def pipeline(self):
        return self._pipeline
