"""Central application state and pipeline parameter definitions."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PipelineParams:
    """All user-configurable pipeline parameters with their defaults."""

    # Segmentation
    lower_threshold: float = 150.0   # HU — minimum intensity to include
    upper_threshold: float = 400.0   # HU — maximum intensity to include
    roi_radius: float = 20.0         # mm — sphere around seed point

    # Segmentation method
    use_levelset: bool = False       # refine threshold with level-set (slower, better)
    levelset_iterations: int = 1000  # level-set evolution iterations
    levelset_curvature: float = 0.7  # curvature scaling (smoothness, 0–2)
    levelset_propagation: float = 1.0 # propagation scaling (expansion speed)

    # Image pre-processing
    resample: float = 2.0            # isotropic resample factor before segmentation

    # Flow extensions
    flow_ext_ratio: float = 5.0      # extension length = ratio × vessel radius
    flow_ext_selected: list = None   # list of profile IDs to extend (None = all)

    # Mesh quality
    reduce_mesh: float = 0.0         # fraction of triangles to remove (0=none, 1=all)
    increase_mesh: int = 0           # Loop subdivision passes (~4x triangles each)

    # Output mode
    build_wall: bool = False         # grow wall outward (for FSI)
    wall_thickness: float = 0.2      # mm — wall thickness when build_wall=True
    solid: bool = False              # watertight solid for 3D printing
    split_patches: bool = False      # split CFD output into wall/cap STLs

    # Seed point
    seed_point_ijk: Optional[tuple] = None # (i, j, k) in image index coordinates

    def copy(self) -> "PipelineParams":
        """Return a shallow copy — safe to pass to workers without sharing refs."""
        return PipelineParams(
            lower_threshold=self.lower_threshold,
            upper_threshold=self.upper_threshold,
            roi_radius=self.roi_radius,
            use_levelset=self.use_levelset,
            levelset_iterations=self.levelset_iterations,
            levelset_curvature=self.levelset_curvature,
            levelset_propagation=self.levelset_propagation,
            resample=self.resample,
            flow_ext_ratio=self.flow_ext_ratio,
            flow_ext_selected=list(self.flow_ext_selected) if self.flow_ext_selected else None,
            reduce_mesh=self.reduce_mesh,
            increase_mesh=self.increase_mesh,
            build_wall=self.build_wall,
            wall_thickness=self.wall_thickness,
            solid=self.solid,
            split_patches=self.split_patches,
            seed_point_ijk=self.seed_point_ijk,
        )


@dataclass
class AppState:
    """Single source of truth for all live pipeline data.

    Workers read a *copy* of params at dispatch time and return results via
    Qt signals.  Workers never write directly into this object.  The main
    thread receives results via signal handlers and stores them here.
    """

    # DICOM selection
    dicom_folder: str = ""
    series_uid: str = ""
    series_description: str = ""

    # Pipeline data (None until the corresponding step completes)
    raw_image: Any = None        # SimpleITK.Image  — original DICOM volume
    vtk_image: Any = None        # vtkImageData     — after sitk→vtk conversion
    levelset_image: Any = None   # vtkImageData     — segmented (threshold or level-set)
    surface: Any = None          # vtkPolyData      — mesh before flow extensions
    centerlines: Any = None      # vtkPolyData      — vessel centerlines
    capped_surface: Any = None   # vtkPolyData      — final watertight mesh (with flow ext)
    boundary_profiles: list = field(default_factory=list)  # [{id, center_mm, radius_mm}]

    # Interaction state
    seed_point_ijk: Optional[tuple] = None   # (i, j, k) in image index coordinates
    seed_point_mm: Optional[tuple] = None    # (x, y, z) in world mm coordinates
    cursor_mm: Optional[tuple] = None        # live cursor position across 3 planes

    # Measurements
    measurements: dict = field(default_factory=dict)

    # User-configurable parameters
    params: PipelineParams = field(default_factory=PipelineParams)

    # ---------------------------------------------------------------------------
    # Convenience helpers
    # ---------------------------------------------------------------------------

    def has_image(self) -> bool:
        return self.raw_image is not None

    def has_surface(self) -> bool:
        return self.surface is not None

    def has_seed(self) -> bool:
        return self.seed_point_ijk is not None

    def has_centerlines(self) -> bool:
        return self.centerlines is not None

    def has_capped_surface(self) -> bool:
        return self.capped_surface is not None

    def reset_pipeline(self) -> None:
        """Clear all derived data while keeping DICOM selection and params."""
        self.vtk_image = None
        self.levelset_image = None
        self.surface = None
        self.centerlines = None
        self.capped_surface = None
        self.boundary_profiles = []
        self.seed_point_ijk = None
        self.seed_point_mm = None
        self.cursor_mm = None
        self.measurements = {}
