#
# PyGEOS ExtensionDType & ExtensionArray
#
import numbers
from collections.abc import Iterable
import numpy as np
import pandas as pd
import pygeos
from pandas.api.extensions import ExtensionArray, ExtensionDtype, register_extension_dtype
from ._shapely import shapely, ShapelyGeometry, PYGEOS_SHAPELY_COMPAT, IGNORE_SHAPELY2_WARNINGS

__all__ = ['GeosDtype', 'GeosArray']


class GeosDtype(ExtensionDtype):
    type = pygeos.lib.Geometry
    name = 'geos'
    na_value = pd.NA

    @classmethod
    def construct_from_string(cls, string):
        if string == cls.name:
            return cls()
        else:
            raise TypeError(
                "Cannot construct a '{}' from '{}'".format(cls.__name__, string)
            )

    @classmethod
    def construct_array_type(cls):
        return GeosArray


register_extension_dtype(GeosDtype)


def _pygeos_to_shapely(geom):
    if geom is None:
        return None

    if PYGEOS_SHAPELY_COMPAT:
        geom = shapely.geos.lgeos.GEOSGeom_clone(geom._ptr)
        return shapely.geometry.base.geom_factory(geom)

    # fallback going through WKB
    if pygeos.is_empty(geom) and pygeos.get_type_id(geom) == 0:
        # empty point does not roundtrip through WKB
        return shapely.wkt.loads("POINT EMPTY")
    else:
        return shapely.wkb.loads(pygeos.to_wkb(geom))


class GeosArray(ExtensionArray):
    _dtype = GeosDtype()
    ndim = 1

    # -------------------------------------------------------------------------
    # (De-)Serialization
    # -------------------------------------------------------------------------

    def __init__(self, data):
        if isinstance(data, self.__class__):
            self.data = data.data
        elif data is None or isinstance(data, self._dtype.type):
            self.data = np.array((data,))
        elif isinstance(data, Iterable) and (data[0] is None or isinstance(data[0], self._dtype.type)):
            self.data = np.asarray(data)
        else:
            raise ValueError(f'Data should be an iterable of {self._dtype.type}')

        self.data[pd.isnull(self.data)] = None

    @classmethod
    def __quickinit__(cls, data):
        pass

    @classmethod
    def from_shapely(cls, data, **kwargs):
        data = pygeos.io.from_shapely(data, **kwargs)
        return cls(data)

    @classmethod
    def from_wkb(cls, data, **kwargs):
        data = pygeos.io.from_wkb(data, **kwargs)
        return cls(data)

    @classmethod
    def from_wkt(cls, data, **kwargs):
        data = pygeos.io.from_wkt(data, **kwargs)
        return cls(data)

    def to_shapely(self):
        out = np.empty(len(self.data), dtype=object)
        with IGNORE_SHAPELY2_WARNINGS():
            out[:] = [_pygeos_to_shapely(g) for g in self.data]
        return out

    def to_wkb(self, **kwargs):
        return pygeos.io.to_wkb(self.data, **kwargs)

    def to_wkt(self, **kwargs):
        return pygeos.io.to_wkt(self.data, **kwargs)

    # -------------------------------------------------------------------------
    # ExtensionArray Specific
    # -------------------------------------------------------------------------

    @classmethod
    def _from_sequence(cls, scalars, dtype=None, copy=False):
        if isinstance(scalars, (str, bytes)) or not isinstance(scalars, Iterable):
            scalars = (scalars,)

        values = np.array(scalars)
        if copy:
            values = values.copy()

        if isinstance(values[0], str):
            return cls.from_wkt(values)
        elif isinstance(values[0], bytes):
            return cls.from_wkb(values)
        elif ShapelyGeometry is not None and isinstance(values[0], ShapelyGeometry):
            return cls.from_shapely(values)

        return cls(values)

    def _values_for_factorize(self):
        return self.data, None

    @classmethod
    def _from_factorized(cls, values, original):
        return cls(values)

    def __getitem__(self, key):
        if isinstance(key, numbers.Integral):
            return self.data[key]

        key = pd.api.indexers.check_array_indexer(self, key)
        if isinstance(key, (Iterable, slice)):
            return GeosArray(self.data[key])
        else:
            raise TypeError("Index type not supported", key)

    def __setitem__(self, key, value):
        key = pd.api.indexers.check_array_indexer(self, key)


    def __len__(self):
        return self.data.shape[0]

    def __eq__(self, other):
        if isinstance(other, (pd.Series, pd.Index, pd.DataFrame)):
            return NotImplemented

        if isinstance(other, self.__class__):
            return self.data == other.data

        return self.data == other

    @property
    def dtype(self):
        return self._dtype

    @property
    def nbytes(self):
        return self.data.nbytes

    def isna(self):
        return pygeos.is_missing(self.data)

    def take(self, indices, allow_fill=False, fill_value=None):
        from pandas.core.algorithms import take

        if allow_fill:
            if fill_value is None or pd.isna(fill_value):
                fill_value = None
            elif not isinstance(fill_value, self._dtype.type):
                raise TypeError("Provide geometry or None as fill value")

        result = take(self.data, indices, allow_fill=allow_fill, fill_value=fill_value)

        if allow_fill and fill_value is None:
            result[pd.isna(result)] = None
        
        return self.__class__(result)

    def copy(self, order='C'):
        return GeosArray(self.data.copy(order))

    def _concat_same_type(self, to_concat):
        data = np.concatenate([c.data for c in to_concat])
        return self.__class__(data)

    def _values_for_argsort(self):
        raise TypeError("geometries are not orderable")

    # -------------------------------------------------------------------------
    # NumPy Specific
    # -------------------------------------------------------------------------

    @property
    def size(self):
        return self.data.size

    @property
    def shape(self):
        return self.data.shape

    def __array__(self, dtype=None):
        return self.data

    # -------------------------------------------------------------------------
    # Custom Methods
    # -------------------------------------------------------------------------
    def affine(self, matrix):
        """
        Performs a 2D or 3D affine transformation on all the coordinates.

        2D
            [x']   / a  b xoff \ [x]
            [y'] = | d  e yoff | [y]
            [1 ]   \ 0  0   1  / [1]

        3D
            [x']   / a  b  c xoff \ [x]
            [y'] = | d  e  f yoff | [y]
            [z']   | g  h  i zoff | [z]
            [1 ]   \ 0  0  0   1  / [1]

        Args:
            matrix (np.ndarray or list-like): Affine transformation matrix.

        Returns:
            pygeospd.GeosArray: Transformed geometries

        Note:
            The transformation matrix can be one of the following types:

            - np.ndarray <3x3 or 2x3>:
              Performs a 2D affine transformation, where the last row of homogeneous coordinates can optionally be discarded.
            - list-like <6>:
              Performs a 2D affine transformation, where the `matrix` represents **(a, b, d, e, xoff, yoff)**.
            - np.ndarray <4x4 or 3x4>:
              Performs a 3D affine transformation, where the last row of homogeneous coordinates can optionally be discarded.
            - list-like <12>:
              Performs a 3D affine transformation, where the `matrix` represents **(a, b, c, d, e, f, g, h, i, xoff, yoff, zoff)**.
        """
        # Get Correct Affine transformation matrix
        if isinstance(matrix, np.ndarray):
            r, c = matrix.shape
            zdim = c == 4
            if r == 2:
                matrix = np.append(matrix, [[0, 0, 1]], axis=0)
            elif c == 4 and r == 3:
                matrix = np.append(matrix, [[0, 0, 0, 1]], axis=0)
        elif len(matrix) == 6:
            zdim = False
            matrix = np.ndarray([
                [matrix[0], matrix[1], matrix[4]],
                [matrix[2], matrix[3], matrix[5]],
                [0,         0,         1],
            ])
        elif len(matrix) == 12:
            zdim = True
            matrix = np.ndarray([
                [matrix[0], matrix[1], matrix[2], matrix[9]],
                [matrix[3], matrix[4], matrix[5], matrix[10]],
                [matrix[6], matrix[7], matrix[8], matrix[11]],
                [0,         0,         0,         1],
            ])

        matrix = matrix[None, ...]

        # Coordinate Function
        def _affine(points):
            points = np.c_[points, np.ones(points.shape[0])][..., None]
            return (matrix @ points)[:, :-1, 0]

        return self.__class__(pygeos.coordinates.apply(self.data, _affine, zdim))

    def __add__(self, other):
        """
        Performs an addition between the coordinates array and other.

        Args:
            other (array-like): Item to add to the coordinates.

        Note:
            When adding the coordinates array and `other`, standard NumPy broadcasting rules apply.
            In order to reduce the friction for users, we decide whether to use the Z-dimension,
            depending on the shape of `other`:

            - `other.shape[-1] == 2`: Do not use Z-dimension.
            - `other.shape[-1] == 3`: Do use Z-dimension.
            - `else`: Use Z-dimension if there are any.
        """
        other = np.asarray(other)
        shape = other.ndim and other.shape[-1]

        if shape == 2:
            zdim = False
        elif shape == 3:
            zdim = True
        else:
            zdim = pygeos.predicates.has_z(self.data).any()

        return self.__class__(pygeos.coordinates.apply(
            self.data,
            lambda pt: pt + other,
            zdim,
        ))

    def __radd__(self, other):
        """
        Performs an right-addition between the coordinates array and other.

        Args:
            other (array-like): Item to add to the coordinates.

        Note:
            When adding the coordinates array and `other`, standard NumPy broadcasting rules apply.
            In order to reduce the friction for users, we decide whether to use the Z-dimension,
            depending on the shape of `other`:

            - `other.shape[-1] == 2`: Do not use Z-dimension.
            - `other.shape[-1] == 3`: Do use Z-dimension.
            - `else`: Use Z-dimension if there are any.
        """
        other = np.asarray(other)
        shape = other.ndim and other.shape[-1]

        if shape == 2:
            zdim = False
        elif shape == 3:
            zdim = True
        else:
            zdim = pygeos.predicates.has_z(self.data).any()

        return self.__class__(pygeos.coordinates.apply(
            self.data,
            lambda pt: other + pt,
            zdim,
        ))

    def __mul__(self, other):
        """
        Performs a multiplication between the coordinates array and other.

        Args:
            other (array-like): Item to add to the coordinates.

        Note:
            When multiplying the coordinates array and `other`, standard NumPy broadcasting rules apply.
            In order to reduce the friction for users, we decide whether to use the Z-dimension,
            depending on the shape of `other`:

            - `other.shape[-1] == 2`: Do not use Z-dimension.
            - `other.shape[-1] == 3`: Do use Z-dimension.
            - `else`: Use Z-dimension if there are any.
        """
        other = np.asarray(other)
        shape = other.ndim and other.shape[-1]

        if shape == 2:
            zdim = False
        elif shape == 3:
            zdim = True
        else:
            zdim = pygeos.predicates.has_z(self.data).any()

        return self.__class__(pygeos.coordinates.apply(
            self.data,
            lambda pt: pt * other,
            zdim,
        ))

    def __rmul__(self, other):
        """
        Performs a right-multiplication between the coordinates array and other.

        Args:
            other (array-like): Item to add to the coordinates.

        Note:
            When multiplying the coordinates array and `other`, standard NumPy broadcasting rules apply.
            In order to reduce the friction for users, we decide whether to use the Z-dimension,
            depending on the shape of `other`:

            - `other.shape[-1] == 2`: Do not use Z-dimension.
            - `other.shape[-1] == 3`: Do use Z-dimension.
            - `else`: Use Z-dimension if there are any.
        """
        other = np.asarray(other)
        shape = other.ndim and other.shape[-1]

        if shape == 2:
            zdim = False
        elif shape == 3:
            zdim = True
        else:
            zdim = pygeos.predicates.has_z(self.data).any()

        return self.__class__(pygeos.coordinates.apply(
            self.data,
            lambda pt: other * pt,
            zdim,
        ))
