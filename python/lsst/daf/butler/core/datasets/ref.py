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

__all__ = ["DatasetRef"]

import hashlib
from typing import Any, Dict, Mapping, Optional, Tuple

from types import MappingProxyType
from ..dimensions import DataCoordinate, DimensionGraph, ExpandedDataCoordinate
from ..configSupport import LookupKey
from ..utils import immutable
from .type import DatasetType


@immutable
class DatasetRef:
    """Reference to a Dataset in a `Registry`.

    A `DatasetRef` may point to a Dataset that currently does not yet exist
    (e.g., because it is a predicted input for provenance).

    Parameters
    ----------
    datasetType : `DatasetType`
        The `DatasetType` for this Dataset.
    dataId : `DataCoordinate`
        A mapping of dimensions that labels the Dataset within a Collection.
    id : `int`, optional
        The unique integer identifier assigned when the dataset is created.
    run : `str`, optional
        The name of the run this dataset was associated with when it was
        created.
    hash : `bytes`, optional
        A hash of the dataset type and data ID.  Should only be provided if
        copying from another `DatasetRef` with the same dataset type and data
        ID.
    components : `dict`, optional
        A dictionary mapping component name to a `DatasetRef` for that
        component.  Should not be passed unless ``id`` is also provided (i.e.
        if this is a "resolved" reference).
    conform : `bool`, optional
        If `True` (default), call `DataCoordinate.standardize` to ensure that
        the data ID's dimensions are consistent with the dataset type's.
        `DatasetRef` instances for which those dimensions are not equal should
        not be created in new code, but are still supported for backwards
        compatibility.  New code should only pass `False` if it can guarantee
        that the dimensions are already consistent.

    Raises
    ------
    ValueError
        Raised if ``run`` or ``components`` is provided but ``id`` is not, or
        if a component dataset is inconsistent with the storage class.
    """

    __slots__ = ("id", "datasetType", "dataId", "run", "_hash", "_components")

    def __new__(cls, datasetType: DatasetType, dataId: DataCoordinate, *,
                id: Optional[int] = None,
                run: Optional[str] = None, hash: Optional[bytes] = None,
                components: Optional[Mapping[str, DatasetRef]] = None, conform: bool = True) -> DatasetRef:
        self = super().__new__(cls)
        assert isinstance(datasetType, DatasetType)
        self.id = id
        self.datasetType = datasetType
        if conform:
            self.dataId = DataCoordinate.standardize(dataId, graph=datasetType.dimensions)
        else:
            self.dataId = dataId
        if self.id is not None:
            self._components = dict()
            if components is not None:
                self._components.update(components)
                for k, v in self._components.items():
                    expectedStorageClass = self.datasetType.storageClass.components.get(k)
                    if expectedStorageClass is None:
                        raise ValueError(f"{k} is not a valid component for "
                                         f"storage class {self.datasetType.storageClass.name}.")
                    if not isinstance(v, DatasetRef):
                        # It's easy to accidentally pass DatasetType or
                        # StorageClass; make that error message friendly.
                        raise ValueError(f"Component {k}={v} is not a DatasetRef.")
                    if v.id is None:
                        raise ValueError(f"DatasetRef components must be resolved ({k}={v} isn't).")
                    if expectedStorageClass != v.datasetType.storageClass:
                        raise ValueError(f"Storage class mismatch for component {k}: "
                                         f"{v.datasetType.storageClass.name} != {expectedStorageClass.name}")
            # TODO: it would be nice to guarantee that id and run should be
            # either both None or not None together.  We can't easily do that
            # yet because the Query infrastructure has a hard time obtaining
            # run strings, so we allow run to be `None` here, but that will
            # change.
            self.run = run
        else:
            self._components = None
            if components:
                raise ValueError("'components' cannot be provided unless 'id' is.")
            if run is not None:
                raise ValueError("'run' cannot be provided unless 'id' is.")
            self.run = None
        if hash is not None:
            # We only set self._hash if we know it; this plays nicely with
            # the @immutable decorator, which allows an attribute to be set
            # only one time.
            self._hash = hash
        return self

    def __eq__(self, other: DatasetRef):
        try:
            return (self.datasetType, self.dataId, self.id) == (other.datasetType, other.dataId, other.id)
        except AttributeError:
            return NotImplemented

    def __hash__(self) -> int:
        return hash(self.datasetType, self.dataId, self.id)

    @property
    def hash(self) -> bytes:
        """Secure hash of the `DatasetType` name and data ID (`bytes`).
        """
        if not hasattr(self, "_hash"):
            message = hashlib.blake2b(digest_size=32)
            message.update(self.datasetType.name.encode("utf8"))
            self.dataId.fingerprint(message.update)
            self._hash = message.digest()
        return self._hash

    @property
    def components(self) -> Optional[Mapping[str, DatasetRef]]:
        """Named `DatasetRef` components (`~collections.abc.Mapping` or
        `None`).

        For resolved `DatasetRef` instances, this is a read-only mapping that
        can be updated in-place via `Registry.attachComponent()`.  For
        unresolved instances, this is always `None`.
        """
        if self._components is None:
            return None
        return MappingProxyType(self._components)

    @property
    def dimensions(self) -> DimensionGraph:
        """The dimensions associated with the underlying `DatasetType`
        """
        return self.datasetType.dimensions

    def __repr__(self) -> str:
        # We delegate to __str__ (i.e use "!s") for the data ID) below because
        # DataCoordinate's __repr__ - while adhering to the guidelines for
        # __repr__ - is much harder to users to read, while its __str__ just
        # produces a dict that can also be passed to DatasetRef's constructor.
        if self.id is not None:
            return (f"DatasetRef({self.datasetType!r}, {self.dataId!s}, id={self.id}, run={self.run!r}, "
                    f"components={self._components})")
        else:
            return f"DatasetRef({self.datasetType!r}, {self.dataId!s})"

    def __str__(self) -> str:
        s = f"{self.datasetType.name}@{self.dataId!s}"
        if self.id is not None:
            s += f" (id={self.id})"
        return s

    def __getnewargs_ex__(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return ((self.datasetType, self.dataId),
                {"id": self.id, "run": self.run, "components": self._components})

    def resolved(self, id: int, run: str, components: Optional[Mapping[str, DatasetRef]] = None
                 ) -> DatasetRef:
        """Return a new `DatasetRef` with the same data ID and dataset type
        and the given ID and run.

        Parameters
        ----------
        id : `int`
            The unique integer identifier assigned when the dataset is created.
        run : `str`
            The run this dataset was associated with when it was created.
        components : `dict`, optional
            A dictionary mapping component name to a `DatasetRef` for that
            component.  If ``self`` is already a resolved `DatasetRef`,
            its components will be merged with this dictionary, with this
            dictionary taking precedence.

        Returns
        -------
        ref : `DatasetRef`
            A new `DatasetRef`.
        """
        if self._components is not None:
            newComponents = self._components.copy()
        else:
            newComponents = {}
        if components:
            newComponents.update(components)
        return DatasetRef(datasetType=self.datasetType, dataId=self.dataId,
                          id=id, run=run, hash=self.hash, components=newComponents, conform=False)

    def unresolved(self) -> DatasetRef:
        """Return a new `DatasetRef` with the same data ID and dataset type,
        but no ID, run, or components.

        Returns
        -------
        ref : `DatasetRef`
            A new `DatasetRef`.

        Notes
        -----
        This can be used to compare only the data ID and dataset type of a
        pair of `DatasetRef` instances, regardless of whether either is
        resolved::

            if ref1.unresolved() == ref2.unresolved():
                ...
        """
        return DatasetRef(datasetType=self.datasetType, dataId=self.dataId, hash=self.hash, conform=False)

    def expanded(self, dataId: ExpandedDataCoordinate) -> DatasetRef:
        """Return a new `DatasetRef` with the given expanded data ID.

        Parameters
        ----------
        dataId : `ExpandedDataCoordinate`
            Data ID for the new `DatasetRef`.  Must compare equal to the
            original data ID.

        Returns
        -------
        ref : `DatasetRef`
            A new `DatasetRef` with the given data ID.
        """
        assert dataId == self.dataId
        return DatasetRef(datasetType=self.datasetType, dataId=dataId,
                          id=self.id, run=self.run, hash=self.hash, components=self.components,
                          conform=False)

    def isComponent(self) -> bool:
        """Boolean indicating whether this `DatasetRef` refers to a
        component of a composite.

        Returns
        -------
        isComponent : `bool`
            `True` if this `DatasetRef` is a component, `False` otherwise.
        """
        return self.datasetType.isComponent()

    def isComposite(self) -> bool:
        """Boolean indicating whether this `DatasetRef` is a composite type.

        Returns
        -------
        isComposite : `bool`
            `True` if this `DatasetRef` is a composite type, `False`
            otherwise.
        """
        return self.datasetType.isComposite()

    def _lookupNames(self) -> Tuple[LookupKey]:
        """Name keys to use when looking up this DatasetRef in a configuration.

        The names are returned in order of priority.

        Returns
        -------
        names : `tuple` of `LookupKey`
            Tuple of the `DatasetType` name and the `StorageClass` name.
            If ``instrument`` is defined in the dataId, each of those names
            is added to the start of the tuple with a key derived from the
            value of ``instrument``.
        """
        # Special case the instrument Dimension since we allow configs
        # to include the instrument name in the hierarchy.
        names = self.datasetType._lookupNames()

        if "instrument" in self.dataId:
            names = tuple(n.clone(dataId={"instrument": self.dataId["instrument"]})
                          for n in names) + names

        return names

    datasetType: DatasetType
    """The definition of this dataset (`DatasetType`).

    Cannot be changed after a `DatasetRef` is constructed.
    """

    dataId: DataCoordinate
    """A mapping of `Dimension` primary key values that labels the dataset
    within a Collection (`DataCoordinate`).

    Cannot be changed after a `DatasetRef` is constructed.
    """

    run: Optional[setattr]
    """The name of the run that produced the dataset.

    Cannot be changed after a `DatasetRef` is constructed; use `resolved` or
    `unresolved` to add or remove this information when creating a new
    `DatasetRef`.
    """

    id: Optional[int]
    """Primary key of the dataset (`int` or `None`).

    Cannot be changed after a `DatasetRef` is constructed; use `resolved` or
    `unresolved` to add or remove this information when creating a new
    `DatasetRef`.
    """
