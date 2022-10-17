"""PhotonOp classes for imsim"""

from functools import lru_cache
import numpy as np

import galsim
import batoid
from batoid import Optic
from galsim import PhotonArray, PhotonOp, GaussianDeviate
from galsim.config import RegisterPhotonOpType, PhotonOpBuilder, GetAllParams

from galsim import PupilAnnulusSampler
from galsim.celestial import CelestialCoord
from galsim.config.util import get_cls_params
from .camera import get_camera
from .utils import focal_to_pixel
import lsst.afw.cameraGeom
from .diffraction import (
    LSST_SPIDER_GEOMETRY,
    apply_diffraction_delta,
)


class LsstOptics(PhotonOp):
    """A photon operator that performs raytracing through the LSST optics.

    Parameters
    ----------
    telescope : batoid.Optic
        The telescope to trace through.
    boresight : galsim.CelestialCoord
        The ICRF coordinate of light that reaches the boresight.  Note that this
        is distinct from the spherical coordinates of the boresight with respect
        to the ICRF axes.
    latitude : float
        Geographic latitude of telescope. Needed to calculate the field rotation
        for the spider diffraction.
    sky_pos : galsim.CelestialCoord
    image_pos : galsim.PositionD
    icrf_to_field : galsim.GSFitsWCS
    det_name : str
    camera : lsst.afw.cameraGeom.Camera
    shift_optics : dict[str, list[float]]
        A dict mapping optics keys to shifts represented by a list of 3 floats.
        The corresponding optics will be displaced by the specified corrdinates.
        Example config for perturbed+defocused telescope to obtain a donut:
        -
            type: lsst_optics
            ...
            shift_optics:
              Detector: [0, 0, 1.5e-3]
              M2: [3.0e-3, 0, 0]
    use_diffraction : bool
        Enable / disable diffraction (default: enabled)
    """

    _req_params = {
        "telescope": Optic,
        "band": str,
        "boresight": CelestialCoord,
        "camera": str,
        "latitude": float,
    }
    _opt_params = {"shift_optics": dict, "use_diffraction": bool}

    def __init__(
        self,
        telescope,
        latitude,
        altitude,
        azimuth,
        boresight,
        sky_pos,
        image_pos,
        icrf_to_field,
        det_name,
        camera,
        shift_optics=None,
        use_diffraction=True,
    ):
        if shift_optics is not None:
            for optics_key, shift in shift_optics.items():
                telescope = telescope.withGloballyShiftedOptic(optics_key, shift)
        self.telescope = telescope
        self.detector = camera[det_name]
        self.latitude = latitude
        self.azimuth = azimuth
        self.altitude = altitude
        self.boresight = boresight
        self.sky_pos = sky_pos
        self.image_pos = image_pos
        self.icrf_to_field = icrf_to_field

        if use_diffraction:
            deviate = GaussianDeviate()

            def diffraction_rng(phi):
                var = phi**2
                deviate.generate_from_variance(var)
                return var

            self.diffraction_rng = diffraction_rng
        else:
            self.diffraction_rng = None

    def applyTo(self, photon_array, local_wcs=None, rng=None):
        """Apply the photon operator to a PhotonArray.

        Note that if the pupil has not yet been sampled (e.g., via
        `imsim.atmPsf.AtmosphericPsf`), then the pupil will be uniformly
        randomly sampled using the Rubin entrance pupil domain.

        Parameters
        ----------
        photon_array:   A `PhotonArray` to apply the operator to.
        local_wcs:      A `LocalWCS` instance defining the local WCS for the current photon
                        bundle in case the operator needs this information.  [default: None]
        rng:            A random number generator to use if needed. [default: None]
        """
        # x/y to u/v
        u = local_wcs._u(photon_array.x, photon_array.y)
        v = local_wcs._v(photon_array.x, photon_array.y)
        # u/v to ICRF
        u_rad = np.deg2rad(u / 3600)
        v_rad = np.deg2rad(v / 3600)
        ra, dec = self.sky_pos.deproject_rad(u_rad, v_rad)
        # ICRF to field
        thx, thy = self.icrf_to_field.radecToxy(ra, dec, units="rad")

        v = np.array(batoid.utils.gnomonicToDirCos(thx, thy)).T
        # Adjust for refractive index of air
        wavelength = photon_array.wavelength * 1e-9
        n = self.telescope.inMedium.getN(wavelength)
        v /= n[:, None]

        if not photon_array.hasAllocatedPupil():
            op = PupilAnnulusSampler(R_inner=2.5, R_outer=4.18)
            op.applyTo(photon_array, None, rng)
        x, y = photon_array.pupil_u, photon_array.pupil_v
        z = self.telescope.stopSurface.surface.sag(x, y)
        if self.diffraction_rng is not None:
            v = apply_diffraction_delta(
                np.c_[x, y],
                v,
                photon_array.time,
                wavelength,
                lat=self.latitude,
                az=self.azimuth,
                alt=self.altitude,
                geometry=LSST_SPIDER_GEOMETRY,
                distribution=self.diffraction_rng,
            )
        ray_vec = batoid.RayVector._directInit(
            x,
            y,
            z,
            v[:, 0],
            v[:, 1],
            v[:, 2],
            t=np.zeros_like(x),
            wavelength=wavelength,
            flux=photon_array.flux,
            vignetted=np.zeros_like(x, dtype=bool),
            failed=np.zeros_like(x, dtype=bool),
            coordSys=self.telescope.stopSurface.coordSys,
        )
        traced = self.telescope.trace(ray_vec)
        ray_vector_to_photon_array(traced, detector=self.detector, out=photon_array)
        photon_array.x -= self.image_pos.x
        photon_array.y -= self.image_pos.y

    def __str__(self):
        return f"imsim.{type(self).__name__}()"

    def __repr__(self):
        return str(self)


def photon_op_type(identifier: str):
    """Decorator which calls RegisterPhotonOpType on a PhotonOp factory,
    defined by a function deserializing a PhotonOp from a dict.

    The decorated function should have the following signature
    > def deserializer(config, base, logger): ...

    config, base and logger will receive the arguments of a call to
    galsim.config.PhotonOpBuilder.buildPhotonOp.
    """

    def decorator(deserializer):
        class Factory(PhotonOpBuilder):
            """Build a PhotonOp generated by a deserializer.

            Returns:
                 the constructed PhotonOp object.
            """

            def buildPhotonOp(self, config, base, logger):
                return deserializer(config, base, logger)

        RegisterPhotonOpType(identifier, Factory())
        return deserializer

    return decorator


def config_kwargs(config, base, cls):
    """Given config and base, extract parameters (including optionals)
    and optional parameters.
    """
    req, opt, single, _takes_rng = get_cls_params(cls)
    kwargs, _safe = GetAllParams(config, base, req, opt, single)
    opt_kwargs = {
        key: kwargs.get(key) for key in cls._opt_params.keys() if key in kwargs
    }
    return kwargs, opt_kwargs


@photon_op_type("lsst_optics")
def deserialize_lsst_optics(config, base, _logger):
    kwargs, opt_kwargs = config_kwargs(config, base, LsstOptics)
    shift_optics = opt_kwargs.pop("shift_optics", base.get("shift_optics", None))

    return LsstOptics(
        telescope=base["_telescope"],
        latitude=kwargs["latitude"],
        altitude=opsim_meta["altitude"],
        azimuth=opsim_meta["azimuth"],
        boresight=kwargs["boresight"],
        sky_pos=base["sky_pos"],
        image_pos=base["image_pos"],
        icrf_to_field=base["_icrf_to_field"],
        det_name=base["det_name"],
        camera=get_camera_cached(kwargs.pop("camera")),
        shift_optics=shift_optics,
        **opt_kwargs,
    )


@lru_cache
def get_camera_cached(camera_name: str):
    return get_camera(camera_name)


def ray_vector_to_photon_array(
    ray_vector: batoid.RayVector, detector, out: PhotonArray
) -> PhotonArray:
    """Converts a batoid.RayVector to a galsim.PhotonArray

    Stores into an already existing galsim.PhotonArray out"""

    w = ~ray_vector.vignetted
    assert all(np.abs(ray_vector.z[w]) < 1.0e-15)
    out.x, out.y = focal_to_pixel(ray_vector.y * 1e3, ray_vector.x * 1e3, detector)

    out.dxdz = ray_vector.vx / ray_vector.vz
    out.dydz = ray_vector.vy / ray_vector.vz
    out.flux[ray_vector.vignetted] = 0.0
    return out
