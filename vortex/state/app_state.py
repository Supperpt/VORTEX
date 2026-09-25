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

    # Surface prep (remesh command) — curvature-adaptive remeshing + smoothing
    remesh_adaptive: bool = True         # size triangles by local curvature (False = uniform edge everywhere, the pre-1.1 behaviour)
    remesh_edge_length: float = 0.5      # mm — COARSEST edge, used on flat parent vessel (0=skip remeshing). Uniform target when remesh_adaptive=False.
    remesh_min_edge_length: float = 0.2  # mm — FINEST edge, used where curvature is high (dome, blebs, neck). Adaptive mode only; must be < remesh_edge_length.
    remesh_smooth_iterations: int = 20   # Taubin iterations in remesh pass (0=skip). More=less noise but rounds off blebs; set 0 after `mesh` (already smooths 30), keep on for load-mesh STLs.

    # Output mode
    build_wall: bool = False         # grow wall outward (for FSI)
    wall_thickness: float = 0.2      # mm — wall thickness when build_wall=True
    solid: bool = False              # watertight solid for 3D printing
    split_patches: bool = False      # split CFD output into wall/cap STLs

    # Seed point
    seed_point_ijk: Optional[tuple] = None # (i, j, k) in image index coordinates

    # Aneurysm sac clipping
    sac_bulge_ratio: float = 1.4     # clip-sac threshold: dist-to-centerline / MISR

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
            remesh_adaptive=self.remesh_adaptive,
            remesh_edge_length=self.remesh_edge_length,
            remesh_min_edge_length=self.remesh_min_edge_length,
            remesh_smooth_iterations=self.remesh_smooth_iterations,
            build_wall=self.build_wall,
            wall_thickness=self.wall_thickness,
            solid=self.solid,
            split_patches=self.split_patches,
            seed_point_ijk=self.seed_point_ijk,
            sac_bulge_ratio=self.sac_bulge_ratio,
        )


@dataclass
class IsolateParams:
    """Parameters for the `isolate` command only.

    Deliberately kept out of PipelineParams. Nothing in the segmentation,
    meshing or export path reads these, so PipelineParams.copy() -- which
    lists every field by hand and sits on the path to every worker -- does
    not need to change, and the existing pipeline cannot regress from a
    change it never sees. It also keeps the `params` table readable; it is
    already 18 rows and these belong to a different question.

    Note `roi_radius` on PipelineParams is a *segmentation* setting and has
    nothing to do with `scaffold_mm` here. They are separate on purpose.
    """

    # Trim length
    n_diameters:       float = 5.0   # parent diameters of vessel kept past the neck

    # Scaffold: the working region that makes centerlines possible.
    # NOT the final trim -- the perpendicular cuts at n_diameters decide that.
    # 15 mm rather than 10 because the measured requirement across AA_001/002/
    # 003/009 is 7.6-12.7 mm from the neck, so 10 truncates every one of them.
    # scaffold_mm is the CAP, not the value used: with scaffold_auto the
    # region grows only while it still contains vessel alone. A fixed size
    # cannot work -- too small truncates the trim, too large takes in bone and
    # the measured "vessel diameter" becomes meaningless, which then sets the
    # trim distance. Measured on an anterior communicating artery case:
    # 12 mm -> 2.25 mm diameter, 15 mm -> 8.93 mm, with triangles going
    # 29,620 -> 109,534 as the skull base entered the box.
    scaffold_mm:       float = 20.0  # upper bound on the working region
    scaffold_auto:     bool  = True  # size it from the data; False pins scaffold_mm
    scaffold_inset_mm: float = 0.6   # inset that turns sealed ends into clean openings

    # Cut geometry
    cut_sphere_factor: float = 2.5   # cut localisation radius, in measured cross-section radii

    # Cleanup and fallbacks
    tear_radius_mm:    float = 1.0   # openings below this are filled as tears
    decimate_target:   float = 0.7   # decimation before the network fallback only
    anchor_retract:    bool  = True  # walk off a sac-intruding branch, network engine only

    def copy(self) -> "IsolateParams":
        """Return a copy — safe to hand to a worker without sharing refs."""
        return IsolateParams(
            n_diameters=self.n_diameters,
            scaffold_mm=self.scaffold_mm,
            scaffold_auto=self.scaffold_auto,
            scaffold_inset_mm=self.scaffold_inset_mm,
            cut_sphere_factor=self.cut_sphere_factor,
            tear_radius_mm=self.tear_radius_mm,
            decimate_target=self.decimate_target,
            anchor_retract=self.anchor_retract,
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
