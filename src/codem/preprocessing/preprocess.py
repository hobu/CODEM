"""
preprocess.py
Project: CRREL-NEGGS University of Houston Collaboration
Date: February 2021

This module contains classes and methods for preparing Point Cloud, Mesh, and
DSM geospatial data types for co-registration. The primary tasks are:

* Estimating data density - this is information is used to set the resolution of
  the data used in the registration modules
* Converting all data types to a DSM - The registration modules operate on a
  gridded version of data being registered. Disorganized data is gridded into a
  DSM, voids are filled, and long wavelength elevation relief removed to allow
  storage of local elevation changes in 8-bit grayscale. 
* Point cloud and normal vector generation - the fine registration module
  requires an array of 3D points and normal vectors for a point-to-plane ICP
  solution. These data are derived from the gridded DSM.

This module contains the following classes and methods:

* GeoData - parent class for geospatial data, NOT to be instantiated directly
* DSM - class for Digital Surface Model data
* PointCloud - class for Point Cloud data
* Mesh - class for Mesh data
* instantiate - method for auto-instantiating the appropriate class
"""
import os
import cv2
import logging
import trimesh
import pdal
import json
import tempfile
import rasterio
import rasterio.fill
import numpy as np
from typing import TypeVar
from rasterio.crs import CRS
from rasterio.enums import Resampling
import codem.lib.resources as r

logger = logging.getLogger(__name__)


class GeoData:
    """
    A class for storing and preparing geospatial data

    Parameters
    ----------
    config: dict
        Dictionary of configuration options
    fnd: bool
        Whether the file is foundation data

    Methods
    -------
    _read_dsm
    _get_nodata_mask
    _infill
    _normalize
    _dsm2pc
    _generate_vectors
    prep
    """

    def __init__(self, config: dict, fnd: bool):
        self.logger = logging.getLogger(__name__)
        self.file = config["FND_FILE"] if fnd else config["AOI_FILE"]
        self.fnd = fnd
        self.type = None
        self.nodata = None
        self.dsm = None
        self.point_cloud = None
        self.crs = None
        self.transform = None
        self.area_or_point = None
        self.normed = None
        self.normal_vectors = None
        self.processed = False
        self.resolution = None
        self.native_resolution = None
        self.units_factor = 1.0
        self.units = None
        self.weak_size = config["DSM_WEAK_FILTER"]
        self.strong_size = config["DSM_STRONG_FILTER"]

    def _read_dsm(self, file_path: str):
        """
        Reads in DSM data from a given file path.

        Parameters
        ----------
        file_path: str
            Path to DSM data
        """
        assert (
            self.resolution is not None
        ), "Resolution must be set prior to preprocessing"

        tag = ["AOI", "Foundation"][int(self.fnd)]

        if self.dsm is None:
            # data = rasterio.open(file_path)
            with rasterio.open(file_path) as data:
                self.dsm = data.read(1)
                self.transform = data.transform
                self.nodata = data.nodata
                self.crs = data.crs

                tags = data.tags()
                if "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Area":
                    self.area_or_point = "Area"
                elif "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Point":
                    self.area_or_point = "Point"
                else:
                    self.area_or_point = "Area"
                    self.logger.debug(
                        f"'AREA_OR_POINT' not supplied in {tag}-{self.type.upper()} - defaulting to 'Area'"
                    )

            # data.close()

        if self.nodata is None:
            self.logger.info(f"{tag}-{self.type.upper()} does not have a nodata value.")
        if (np.array(self.transform) == np.identity(3).flatten()).all():
            self.logger.warning(f"{tag}-{self.type.upper()} has an identity transform.")

    def _get_nodata_mask(self, dsm: np.array) -> np.array:
        """
        Generates a binary array indicating invalid data locations in the
        passed array. Invalid data are NaN and nodata values. A value of '1'
        indicates valid data locations. '0' indicates invalid data locations.

        Parameters
        ----------
        dsm: np.array
            Array containing digital surface model elevation data

        Returns
        -------
        mask: np.array
            The binary mask
        """
        nan_mask = np.isnan(dsm)
        if self.nodata is not None:
            dsm[nan_mask] = self.nodata
            mask = dsm != self.nodata
        else:
            mask = ~nan_mask

        return mask.astype(np.uint8)

    def _infill(self):
        """
        Infills pixels flagged as invalid (via the nodata value or NaN values)
        via rasterio's inverse distance weighting interpolation. Necessary to
        mitigate spurious feature detection.
        """
        dsm_array = np.array(self.dsm)
        if self.nodata is not None:
            empty_array = np.full(dsm_array.shape, self.nodata)
        else:
            empty_array = np.empty_like(dsm_array)

        assert not np.array_equal(dsm_array, empty_array), "DSM array is empty."

        infilled = np.copy(self.dsm)
        mask = self._get_nodata_mask(infilled)
        infill_mask = np.copy(mask)

        while np.sum(infill_mask) < infill_mask.size:
            infilled = rasterio.fill.fillnodata(infilled, mask=infill_mask)
            infill_mask = self._get_nodata_mask(infilled)

        self.infilled = infilled
        self.nodata_mask = mask

    def _normalize(self):
        """
        Suppresses high frequency information and removes long wavelength
        topography with a bandpass filter. Normalizes the result to fit in an
        8-bit range. We scale the strong and weak filter sizes to convert them
        from object space distance to pixels.
        """
        scale = np.sqrt(self.transform[0] ** 2 + self.transform[1] ** 2)
        weak_filtered = cv2.GaussianBlur(self.infilled, (0, 0), self.weak_size / scale)
        strong_filtered = cv2.GaussianBlur(
            self.infilled, (0, 0), self.strong_size / scale
        )
        bandpassed = weak_filtered - strong_filtered

        low = np.percentile(bandpassed, 1)
        high = np.percentile(bandpassed, 99)
        clipped = np.clip(bandpassed, low, high)
        normalized = (clipped - low) / (high - low)
        quantized = np.uint8(255 * normalized)
        self.normed = quantized

    def _dsm2pc(self):
        """
        Converts DSM data to point cloud data. If the DSM was saved with the
        AREA_OR_POINT tag set to 'Area', then we adjust the pixel values by 0.5
        pixel. This is because we assume the DSM elevation value to represent
        the elevation at the center of the pixel, not the upper left corner.
        """
        rows = np.arange(self.dsm.shape[0], dtype=np.float64)
        cols = np.arange(self.dsm.shape[1], dtype=np.float64)
        uu, vv = np.meshgrid(cols, rows)
        u = np.reshape(uu, -1)
        v = np.reshape(vv, -1)

        if self.area_or_point == "Area":
            u += 0.5
            v += 0.5

        xy = np.asarray(self.transform * (u, v))
        z = np.reshape(self.dsm, -1)
        xyz = np.vstack((xy, z)).T

        mask = np.reshape(np.array(self.nodata_mask, dtype=bool), -1)
        xyz = xyz[mask]

        self.point_cloud = xyz

    def _generate_vectors(self):
        """
        Generates normal vectors, required for the ICP registration module, from
        the point cloud data. PDAL is used for speed.
        """
        k = 9
        n_points = self.point_cloud.shape[0]
        assert (
            n_points >= k
        ), f"Point cloud must have at least {k} points to generate normal vectors."

        xyz_dtype = np.dtype([("X", np.double), ("Y", np.double), ("Z", np.double)])
        xyz = np.empty(self.point_cloud.shape[0], dtype=xyz_dtype)
        xyz["X"] = self.point_cloud[:, 0]
        xyz["Y"] = self.point_cloud[:, 1]
        xyz["Z"] = self.point_cloud[:, 2]
        pipe = [
            {"type": "filters.normal", "knn": k},
        ]
        p = pdal.Pipeline(
            json.dumps(pipe),
            arrays=[
                xyz,
            ],
        )
        p.execute()

        arrays = p.arrays
        array = arrays[0]
        filtered_normals = np.vstack(
            (array["NormalX"], array["NormalY"], array["NormalZ"])
        ).T
        self.normal_vectors = filtered_normals

    def _calculate_resolution(self):
        ...

    def _create_dsm(self):
        ...

    def prep(self):
        """
        Prepares data for registration.
        """
        tag = ["AOI", "Foundation"][int(self.fnd)]
        self.logger.info(f"Preparing {tag}-{self.type.upper()} for registration.")
        self._create_dsm()
        self._infill()
        self._normalize()
        self._dsm2pc()

        if self.fnd:
            self._generate_vectors()

        self.processed = True


G = TypeVar("G", bound=GeoData)


class DSM(GeoData):
    """
    A class for storing and preparing Digital Surface Model (DSM) data.
    """

    def __init__(self, config: dict, fnd: bool):
        super().__init__(config, fnd)
        self.type = "dsm"
        self._calculate_resolution()

    def _create_dsm(self):
        """
        Resamples the DSM to the registration pipeline resolution and applies
        a scale factor to convert to meters.
        """
        data = rasterio.open(self.file)
        resample_factor = self.native_resolution / self.resolution
        tag = ["AOI", "Foundation"][int(self.fnd)]
        if resample_factor != 1:
            self.logger.info(
                f"Resampling {tag}-{self.type.upper()} to a pixel resolution of: {self.resolution} meters"
            )
            self.dsm = data.read(
                1,
                out_shape=(
                    data.count,
                    int(data.height * resample_factor),
                    int(data.width * resample_factor),
                ),
                resampling=Resampling.cubic,
            )
            # We post-multiply the transform by the resampling scale. This does
            # not change the origin coordinates, only the pixel scale.
            self.transform = data.transform * data.transform.scale(
                (data.width / self.dsm.shape[-1]),
                (data.height / self.dsm.shape[-2]),
            )
        else:
            self.logger.info(f"No resampling required for {tag}-{self.type.upper()}")
            self.dsm = data.read(1)
            self.transform = data.transform

        # Scale the elevation values into meters
        mask = (self._get_nodata_mask(self.dsm)).astype(bool)
        self.dsm[mask] *= self.units_factor

        # We pre-multiply the transform by the unit change scale. This scales
        # the origin coordinates into meters and also changes the pixel scale
        # into meters.
        self.transform = (
            data.transform.scale(self.units_factor, self.units_factor) * self.transform
        )

        self.nodata = data.nodata
        self.crs = data.crs
        tags = data.tags()
        if "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Area":
            self.area_or_point = "Area"
        elif "AREA_OR_POINT" in tags and tags["AREA_OR_POINT"] == "Point":
            self.area_or_point = "Point"
        else:
            self.area_or_point = "Area"
            self.logger.debug(
                f"'AREA_OR_POINT' not supplied in {tag}-{self.type.upper()} - defaulting to 'Area'"
            )

        data.close()

        if self.nodata is None:
            self.logger.info(f"{tag}-{self.type.upper()} does not have a nodata value.")
        if (np.array(self.transform) == np.identity(3).flatten()).all():
            self.logger.warning(f"{tag}-{self.type.upper()} has an identity transform.")

    def _calculate_resolution(self):
        """
        Calculates the pixel resolution of the DSM file.
        """
        data = rasterio.open(self.file)
        T = data.transform
        A = np.array(T).reshape(3, 3)[0:2, 0:2]
        assert np.all(
            A == np.diag(np.diagonal(A))
        ), "Raster transforms cannot contain a rotation angle."
        assert np.trace(A) == 0, "X scale and Y scale must be identical."

        tag = ["AOI", "Foundation"][int(self.fnd)]
        if data.crs is None:
            self.logger.warning(
                f"Linear unit for {tag}-{self.type.upper()} not detected --> meters assumed"
            )
            px_res = np.abs(T[0])
        else:
            self.logger.info(
                f"Linear unit for {tag}-{self.type.upper()} detected as {data.crs.linear_units}"
            )
            px_res = np.abs(T[0]) * data.crs.linear_units_factor[1]
            self.units_factor = data.crs.linear_units_factor[1]
            self.units = data.crs.linear_units

        resolution = round(px_res, 1)
        self.logger.info(
            f"Calculated native resolution of {tag}-{self.type.upper()} as: {resolution} meters"
        )

        self.native_resolution = resolution


class PointCloud(GeoData):
    """
    A class for storing and preparing Point Cloud data.
    """

    def __init__(self, config: dict, fnd: bool):
        super().__init__(config, fnd)
        self.type = "pcloud"
        self._calculate_resolution()

    def _create_dsm(self):
        """
        Converts the point cloud to meters and rasters it to a DSM.
        """
        assert (
            self.resolution is not None
        ), "Resolution must be set prior to preprocessing"

        tag = ["AOI", "Foundation"][int(self.fnd)]
        self.logger.info(
            f"Extracting DSM from {tag}-{self.type.upper()} with resolution of: {self.resolution} meters"
        )

        # Scale matrix formatted for PDAL consumption
        units_transform = "{} 0 0 0 0 {} 0 0 0 0 {} 0 0 0 0 1".format(
            self.units_factor, self.units_factor, self.units_factor
        )
        file_handle, tmp_file = tempfile.mkstemp(suffix=".tif")

        pipe = [
            self.file,
            {"type": "filters.transformation", "matrix": units_transform},
            {
                "type": "writers.gdal",
                "resolution": self.resolution,
                "output_type": "max",
                "nodata": -9999.0,
                "filename": tmp_file,
            },
        ]

        p = pdal.Pipeline(json.dumps(pipe))
        p.execute()

        self._read_dsm(tmp_file)
        os.close(file_handle)
        os.remove(tmp_file)

    def _calculate_resolution(self):
        """
        Calculates point cloud average point spacing.
        """
        pipeline = [
            self.file,
            {"type": "filters.hexbin", "edge_size": 25, "threshold": 1},
        ]
        pipeline = pdal.Pipeline(json.dumps(pipeline))
        pipeline.execute()

        tag = ["AOI", "Foundation"][int(self.fnd)]
        # metadata = json.loads(pipeline.metadata)["metadata"]
        metadata = pipeline.metadata["metadata"]
        reader_metadata = [val for key, val in metadata.items() if "readers" in key]
        if reader_metadata[0]["srs"]["horizontal"] == "":
            self.logger.warning(
                f"Linear unit for {tag}-{self.type.upper()} not detected --> meters assumed"
            )
            spacing = metadata["filters.hexbin"]["avg_pt_spacing"]
        else:
            crs = CRS.from_string(reader_metadata[0]["srs"]["horizontal"])
            self.logger.info(
                f"Linear unit for {tag}-{self.type.upper()} detected as {crs.linear_units}."
            )
            spacing = (
                crs.linear_units_factor[1]
                * metadata["filters.hexbin"]["avg_pt_spacing"]
            )
            self.units_factor = crs.linear_units_factor[1]
            self.units = crs.linear_units

        resolution = round(spacing, 1)
        self.logger.info(
            f"Calculated native resolution for {tag}-{self.type.upper()} as: {resolution} meters"
        )

        self.native_resolution = resolution


class Mesh(GeoData):
    """
    A class for storing and preparing Mesh data.
    """

    def __init__(self, config: dict, fnd: bool):
        super().__init__(config, fnd)
        self.type = "mesh"
        self._calculate_resolution()

    def _create_dsm(self):
        """
        Converts mesh vertices to meters and rasters them to a DSM.
        """
        assert (
            self.resolution is not None
        ), "Resolution must be set prior to preprocessing"

        tag = ["AOI", "Foundation"][int(self.fnd)]
        self.logger.info(
            f"Extracting DSM from {tag}-{self.type.upper()} with resolution of: {self.resolution} meters"
        )

        mesh = trimesh.load_mesh(self.file)
        vertices = mesh.vertices

        xyz_dtype = np.dtype([("X", np.double), ("Y", np.double), ("Z", np.double)])
        xyz = np.empty(vertices.shape[0], dtype=xyz_dtype)
        xyz["X"] = vertices[:, 0]
        xyz["Y"] = vertices[:, 1]
        xyz["Z"] = vertices[:, 2]

        # Scale matrix formatted for PDAL consumption
        units_transform = "{} 0 0 0 0 {} 0 0 0 0 {} 0 0 0 0 1".format(
            self.units_factor, self.units_factor, self.units_factor
        )
        pipe = [
            self.file,
            {
                "type": "filters.transformation",
                "matrix": units_transform,
            },
            {
                "type": "writers.gdal",
                "resolution": self.resolution,
                "output_type": "max",
                "nodata": -9999.0,
                "filename": "temp_dsm.tif",
            },
        ]
        p = pdal.Pipeline(
            json.dumps(pipe),
            arrays=[
                xyz,
            ],
        )
        p.execute()

        self._read_dsm("temp_dsm.tif")
        os.remove("temp_dsm.tif")

    def _calculate_resolution(self):
        """
        Calculates mesh average vertex spacing.
        """
        pipeline = [
            self.file,
            {"type": "filters.hexbin", "edge_size": 25, "threshold": 1},
        ]
        pipeline = pdal.Pipeline(json.dumps(pipeline))
        pipeline.execute()
        # metadata = json.loads(pipeline.metadata)["metadata"]
        metadata = pipeline.metadata["metadata"]
        spacing = metadata["filters.hexbin"]["avg_pt_spacing"]

        mesh = trimesh.load_mesh(self.file)
        tag = ["AOI", "Foundation"][int(self.fnd)]
        if mesh.units is None:
            self.logger.warning(
                f"Linear unit for {tag}-{self.type.upper()} not detected --> meters assumed"
            )
        else:
            self.logger.info(
                f"Linear unit for {tag}-{self.type.upper()} detected as {mesh.units}"
            )
            self.units_factor = trimesh.units.unit_conversion(mesh.units, "meters")
            self.units = mesh.units
            spacing *= self.units_factor

        resolution = round(spacing, 1)
        self.logger.info(
            f"Calculated native resolution for {tag}-{self.type.upper()} as: {resolution} meters"
        )

        self.native_resolution = resolution


def instantiate(config: dict, fnd: bool) -> G:
    """
    Factory method for auto-instantiating the appropriate data class.

    Parameters
    ----------
    file_path: str
        Path to data file
    fnd: bool
        Whether the file is the foundation object

    Returns
    -------
    G: Type[G]
        An instance of the appropriate child class of GeoData
    """
    file_path = config["FND_FILE"] if fnd else config["AOI_FILE"]
    if os.path.splitext(file_path)[-1] in r.dsm_filetypes:
        return DSM(config, fnd)
    if os.path.splitext(file_path)[-1] in r.mesh_filetypes:
        return Mesh(config, fnd)
    if os.path.splitext(file_path)[-1] in r.pcloud_filetypes:
        return PointCloud(config, fnd)
    else:
        logger.warning(f"File {file_path} has an unsupported type.")
        raise NotImplementedError("File type not currently supported.")