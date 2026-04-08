"""
VTK compatibility shim.

VMTK ships its own bundled VTK. Depending on the vmtk version, the VTK
namespace may be exposed as either `vtk` (older vmtk) or `vtkmodules`
(newer VTK 9+). This module abstracts that difference so the rest of the
codebase always uses `vtk` from here.

Also provides:
  - sitk_to_vtk()    : SimpleITK image → vtkImageData
  - vtk_to_numpy()   : vtkImageData → numpy array (zyx order)
  - get_slice_array(): extract a 2-D numpy slice from a vtkImageData
"""

import numpy as np

# ---------------------------------------------------------------------------
# VTK import shim
# ---------------------------------------------------------------------------
try:
    import vtkmodules.all as vtk  # VTK 9+ style (newer vmtk builds)
    from vtkmodules.util import numpy_support as vtk_np
except ImportError:
    try:
        import vtk  # older vmtk / standalone vtk
        from vtk.util import numpy_support as vtk_np
    except ImportError as exc:
        raise ImportError(
            "VTK not found. Make sure vmtk is installed in the active venv. "
            "Do NOT 'pip install vtk' separately — vmtk bundles its own VTK."
        ) from exc

__all__ = [
    "vtk",
    "vtk_np",
    "sitk_to_vtk",
    "vtk_to_numpy",
    "get_slice_array",
]


# ---------------------------------------------------------------------------
# SimpleITK → vtkImageData
# ---------------------------------------------------------------------------

def sitk_to_vtk(sitk_image):
    """Convert a SimpleITK image to a vtkImageData object.

    The pixel data is copied into a VTK array via numpy without any
    additional library dependencies.

    Parameters
    ----------
    sitk_image : SimpleITK.Image
        3-D image (any scalar type).

    Returns
    -------
    vtk.vtkImageData
    """
    import SimpleITK as sitk

    array = sitk.GetArrayFromImage(sitk_image)  # shape: (z, y, x), numpy
    spacing = sitk_image.GetSpacing()            # (sx, sy, sz)
    origin  = sitk_image.GetOrigin()             # (ox, oy, oz)

    vtk_image = vtk.vtkImageData()
    vtk_image.SetDimensions(array.shape[2], array.shape[1], array.shape[0])
    vtk_image.SetSpacing(spacing[0], spacing[1], spacing[2])
    vtk_image.SetOrigin(origin[0], origin[1], origin[2])

    flat = array.ravel(order='C').astype(np.float32)
    vtk_array = vtk_np.numpy_to_vtk(flat, deep=True, array_type=vtk.VTK_FLOAT)
    vtk_array.SetName("Scalars")

    vtk_image.GetPointData().SetScalars(vtk_array)
    return vtk_image


# ---------------------------------------------------------------------------
# vtkImageData → numpy
# ---------------------------------------------------------------------------

def vtk_to_numpy(vtk_image):
    """Return a 3-D numpy array (z, y, x) from a vtkImageData scalar field."""
    dims = vtk_image.GetDimensions()  # (nx, ny, nz)
    scalars = vtk_image.GetPointData().GetScalars()
    array = vtk_np.vtk_to_numpy(scalars)
    return array.reshape(dims[2], dims[1], dims[0])


# ---------------------------------------------------------------------------
# Axis slice extractor
# ---------------------------------------------------------------------------

def get_slice_array(vtk_image, axis: int, index: int) -> np.ndarray:
    """Extract a 2-D numpy array from a vtkImageData along a given axis.

    Parameters
    ----------
    vtk_image : vtk.vtkImageData
    axis  : 0=axial (z), 1=coronal (y), 2=sagittal (x)
    index : slice index along the axis

    Returns
    -------
    np.ndarray  shape depends on axis:
        axis 0 → (ny, nx)
        axis 1 → (nz, nx)
        axis 2 → (nz, ny)
    """
    vol = vtk_to_numpy(vtk_image)  # (nz, ny, nx)
    if axis == 0:
        return vol[index, :, :]
    elif axis == 1:
        return vol[:, index, :]
    elif axis == 2:
        return vol[:, :, index]
    else:
        raise ValueError(f"axis must be 0, 1, or 2, got {axis}")
