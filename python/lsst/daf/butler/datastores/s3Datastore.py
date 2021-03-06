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

"""S3 datastore."""

__all__ = ("S3Datastore", )

import boto3
import logging
import os
import pathlib
import tempfile

from typing import Optional, Type

from lsst.daf.butler import (
    ButlerURI,
    DatasetRef,
    Formatter,
    Location,
    StoredFileInfo,
)

from .fileLikeDatastore import FileLikeDatastore
from lsst.daf.butler.core.s3utils import s3CheckFileExists, bucketExists
from lsst.daf.butler.core.utils import transactional

log = logging.getLogger(__name__)


class S3Datastore(FileLikeDatastore):
    """Basic S3 Object Storage backed Datastore.

    Parameters
    ----------
    config : `DatastoreConfig` or `str`
        Configuration. A string should refer to the name of the config file.
    registry : `Registry`
        Registry to use for storing internal information about the datasets.
    butlerRoot : `str`, optional
        New datastore root to use to override the configuration value.

    Raises
    ------
    ValueError
        If root location does not exist and ``create`` is `False` in the
        configuration.

    Notes
    -----
    S3Datastore supports non-link transfer modes for file-based ingest:
    `"move"`, `"copy"`, and `None` (no transfer).
    """

    defaultConfigFile = "datastores/s3Datastore.yaml"
    """Path to configuration defaults. Relative to $DAF_BUTLER_DIR/config or
    absolute path. Can be None if no defaults specified.
    """

    def __init__(self, config, registry, butlerRoot=None):
        super().__init__(config, registry, butlerRoot)

        self.client = boto3.client("s3")
        if not bucketExists(self.locationFactory.netloc):
            # PosixDatastore creates the root directory if one does not exist.
            # Calling s3 client.create_bucket is possible but also requires
            # ACL LocationConstraints, Permissions and other configuration
            # parameters, so for now we do not create a bucket if one is
            # missing. Further discussion can make this happen though.
            raise IOError(f"Bucket {self.locationFactory.netloc} does not exist!")

    def exists(self, ref):
        """Check if the dataset exists in the datastore.

        Parameters
        ----------
        ref : `DatasetRef`
            Reference to the required dataset.

        Returns
        -------
        exists : `bool`
            `True` if the entity exists in the `Datastore`.
        """
        location, _ = self._get_dataset_location_info(ref)
        if location is None:
            return False
        return s3CheckFileExists(location, client=self.client)[0]

    def get(self, ref, parameters=None):
        """Load an InMemoryDataset from the store.

        Parameters
        ----------
        ref : `DatasetRef`
            Reference to the required Dataset.
        parameters : `dict`
            `StorageClass`-specific parameters that specify, for example,
            a slice of the Dataset to be loaded.

        Returns
        -------
        inMemoryDataset : `object`
            Requested Dataset or slice thereof as an InMemoryDataset.

        Raises
        ------
        FileNotFoundError
            Requested dataset can not be retrieved.
        TypeError
            Return value from formatter has unexpected type.
        ValueError
            Formatter failed to process the dataset.
        """
        getInfo = self._prepare_for_get(ref, parameters)
        location = getInfo.location

        # since we have to make a GET request to S3 anyhow (for download) we
        # might as well use the HEADER metadata for size comparison instead.
        # s3CheckFileExists would just duplicate GET/LIST charges in this case.
        try:
            response = self.client.get_object(Bucket=location.netloc,
                                              Key=location.relativeToPathRoot)
        except self.client.exceptions.ClientError as err:
            errorcode = err.response["ResponseMetadata"]["HTTPStatusCode"]
            # head_object returns 404 when object does not exist only when user
            # has s3:ListBucket permission. If list permission does not exist a
            # 403 is returned. In practical terms this usually means that the
            # file does not exist, but it could also mean user lacks GetObject
            # permission. It's hard to tell which case is it.
            # docs.aws.amazon.com/AmazonS3/latest/API/RESTObjectHEAD.html
            # Unit tests right now demand FileExistsError is raised, but this
            # should be updated to PermissionError like in s3CheckFileExists.
            if errorcode == 403:
                raise FileNotFoundError(f"Dataset with Id {ref.id} not accessible at "
                                        f"expected location {location}. Forbidden HEAD "
                                        "operation error occured. Verify s3:ListBucket "
                                        "and s3:GetObject permissions are granted for "
                                        "your IAM user and that file exists. ") from err
            if errorcode == 404:
                errmsg = f"Dataset with Id {ref.id} does not exists at expected location {location}."
                raise FileNotFoundError(errmsg) from err
            # other errors are reraised also, but less descriptively
            raise err

        storedFileInfo = getInfo.info
        if response["ContentLength"] != storedFileInfo.file_size:
            raise RuntimeError("Integrity failure in Datastore. Size of file {} ({}) does not"
                               " match recorded size of {}".format(location.path, response["ContentLength"],
                                                                   storedFileInfo.file_size))

        # download the data as bytes
        serializedDataset = response["Body"].read()

        # format the downloaded bytes into appropriate object directly, or via
        # tempfile (when formatter does not support to/from/Bytes). This is S3
        # equivalent of PosixDatastore formatter.read try-except block.
        formatter = getInfo.formatter
        try:
            result = formatter.fromBytes(serializedDataset, component=getInfo.component)
        except NotImplementedError:
            with tempfile.NamedTemporaryFile(suffix=formatter.extension) as tmpFile:
                tmpFile.file.write(serializedDataset)
                formatter._fileDescriptor.location = Location(*os.path.split(tmpFile.name))
                result = formatter.read(component=getInfo.component)
        except Exception as e:
            raise ValueError(f"Failure from formatter for Dataset {ref.id}: {e}") from e

        return self._post_process_get(result, getInfo.readStorageClass, getInfo.assemblerParams)

    @transactional
    def put(self, inMemoryDataset, ref):
        """Write a InMemoryDataset with a given `DatasetRef` to the store.

        Parameters
        ----------
        inMemoryDataset : `object`
            The Dataset to store.
        ref : `DatasetRef`
            Reference to the associated Dataset.

        Raises
        ------
        TypeError
            Supplied object and storage class are inconsistent.
        DatasetTypeNotSupportedError
            The associated `DatasetType` is not handled by this datastore.

        Notes
        -----
        If the datastore is configured to reject certain dataset types it
        is possible that the put will fail and raise a
        `DatasetTypeNotSupportedError`.  The main use case for this is to
        allow `ChainedDatastore` to put to multiple datastores without
        requiring that every datastore accepts the dataset.
        """
        location, formatter = self._prepare_for_put(inMemoryDataset, ref)

        # in PosixDatastore a directory can be created by `safeMakeDir`. In S3
        # `Keys` instead only look like directories, but are not. We check if
        # an *exact* full key already exists before writing instead. The insert
        # key operation is equivalent to creating the dir and the file.
        location.updateExtension(formatter.extension)
        if s3CheckFileExists(location, client=self.client,)[0]:
            raise FileExistsError(f"Cannot write file for ref {ref} as "
                                  f"output file {location.uri} exists.")

        # upload the file directly from bytes or by using a temporary file if
        # _toBytes is not implemented
        try:
            serializedDataset = formatter.toBytes(inMemoryDataset)
            self.client.put_object(Bucket=location.netloc, Key=location.relativeToPathRoot,
                                   Body=serializedDataset)
            log.debug("Wrote file directly to %s", location.uri)
        except NotImplementedError:
            with tempfile.NamedTemporaryFile(suffix=formatter.extension) as tmpFile:
                formatter._fileDescriptor.location = Location(*os.path.split(tmpFile.name))
                formatter.write(inMemoryDataset)
                self.client.upload_file(Bucket=location.netloc, Key=location.relativeToPathRoot,
                                        Filename=tmpFile.name)
                log.debug("Wrote file to %s via a temporary directory.", location.uri)

        # Register a callback to try to delete the uploaded data if
        # the ingest fails below
        self._transaction.registerUndo("write", self.client.delete_object,
                                       Bucket=location.netloc, Key=location.relativeToPathRoot)

        # URI is needed to resolve what ingest case are we dealing with
        info = self._extractIngestInfo(location.uri, ref, formatter=formatter)
        self._register_datasets([(ref, info)])

    def _standardizeIngestPath(self, path: str, *, transfer: Optional[str] = None) -> str:
        # Docstring inherited from FileLikeDatastore._standardizeIngestPath.
        if transfer not in (None, "move", "copy"):
            raise NotImplementedError(f"Transfer mode {transfer} not supported.")
        # ingest can occur from file->s3 and s3->s3 (source can be file or s3,
        # target will always be s3). File has to exist at target location. Two
        # Schemeless URIs are assumed to obey os.path rules. Equivalent to
        # os.path.exists(fullPath) check in PosixDatastore.
        srcUri = ButlerURI(path)
        if srcUri.scheme == 'file' or not srcUri.scheme:
            if not os.path.exists(srcUri.ospath):
                raise FileNotFoundError(f"File at '{srcUri}' does not exist.")
        elif srcUri.scheme == 's3':
            if not s3CheckFileExists(srcUri, client=self.client)[0]:
                raise FileNotFoundError(f"File at '{srcUri}' does not exist.")
        else:
            raise NotImplementedError(f"Scheme type {srcUri.scheme} not supported.")

        if transfer is None:
            rootUri = ButlerURI(self.root)
            if srcUri.scheme == "file":
                raise RuntimeError(f"'{srcUri}' is not inside repository root '{rootUri}'. "
                                   "Ingesting local data to S3Datastore without upload "
                                   "to S3 is not allowed.")
            elif srcUri.scheme == "s3":
                if not srcUri.path.startswith(rootUri.path):
                    raise RuntimeError(f"'{srcUri}' is not inside repository root '{rootUri}'.")
        return path

    def _extractIngestInfo(self, path: str, ref: DatasetRef, *, formatter: Type[Formatter],
                           transfer: Optional[str] = None) -> StoredFileInfo:
        # Docstring inherited from FileLikeDatastore._extractIngestInfo.
        srcUri = ButlerURI(path)
        if transfer is None:
            rootUri = ButlerURI(self.root)
            p = pathlib.PurePosixPath(srcUri.relativeToPathRoot)
            pathInStore = str(p.relative_to(rootUri.relativeToPathRoot))
            tgtLocation = self.locationFactory.fromPath(pathInStore)
        else:
            assert transfer == "move" or transfer == "copy", "Should be guaranteed by _standardizeIngestPath"
            if srcUri.scheme == "file":
                # source is on local disk.
                template = self.templates.getTemplate(ref)
                location = self.locationFactory.fromPath(template.format(ref))
                tgtPathInStore = formatter.predictPathFromLocation(location)
                tgtLocation = self.locationFactory.fromPath(tgtPathInStore)
                self.client.upload_file(Bucket=tgtLocation.netloc, Key=tgtLocation.relativeToPathRoot,
                                        Filename=srcUri.ospath)
                if transfer == "move":
                    os.remove(srcUri.ospath)
            elif srcUri.scheme == "s3":
                # source is another S3 Bucket
                relpath = srcUri.relativeToPathRoot
                copySrc = {"Bucket": srcUri.netloc, "Key": relpath}
                self.client.copy(copySrc, self.locationFactory.netloc, relpath)
                if transfer == "move":
                    # https://github.com/boto/boto3/issues/507 - there is no
                    # way of knowing if the file was actually deleted except
                    # for checking all the keys again, reponse is  HTTP 204 OK
                    # response all the time
                    self.client.delete(Bucket=srcUri.netloc, Key=relpath)
                p = pathlib.PurePosixPath(srcUri.relativeToPathRoot)
                relativeToDatastoreRoot = str(p.relative_to(rootUri.relativeToPathRoot))
                tgtLocation = self.locationFactory.fromPath(relativeToDatastoreRoot)

        # the file should exist on the bucket by now
        exists, size = s3CheckFileExists(path=tgtLocation.relativeToPathRoot,
                                         bucket=tgtLocation.netloc,
                                         client=self.client)

        return StoredFileInfo(formatter=formatter, path=tgtLocation.pathInStore,
                              storageClass=ref.datasetType.storageClass,
                              file_size=size, checksum=None)

    def remove(self, ref):
        """Indicate to the Datastore that a Dataset can be removed.

        .. warning::

            This method does not support transactions; removals are
            immediate, cannot be undone, and are not guaranteed to
            be atomic if deleting either the file or the internal
            database records fails.

        Parameters
        ----------
        ref : `DatasetRef`
            Reference to the required Dataset.

        Raises
        ------
        FileNotFoundError
            Attempt to remove a dataset that does not exist.
        """
        location, _ = self._get_dataset_location_info(ref)
        if location is None:
            raise FileNotFoundError(f"Requested dataset ({ref}) does not exist")

        if not s3CheckFileExists(location, client=self.client):
            raise FileNotFoundError(f"No such file: {location.uri}")

        if self._can_remove_dataset_artifact(ref):
            # https://github.com/boto/boto3/issues/507 - there is no way of
            # knowing if the file was actually deleted
            self.client.delete_object(Bucket=location.netloc, Key=location.relativeToPathRoot)

        # Remove rows from registries
        self._remove_from_registry(ref)
