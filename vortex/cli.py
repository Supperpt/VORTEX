"""VORTEX Aneurysm — Elaborate CLI & Interactive Shell.

Run via:
    python -m vortex.cli [args]
or:
    ./run-cli.sh shell
"""

import argparse
import logging
import sys
import os
import time
from typing import Optional, Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, Confirm
from rich import print as rprint
from rich import box

from vortex.utils.logging_config import setup_logging, get_logger
from vortex.state.app_state import PipelineParams
from vortex.pipeline.dicom_loader import list_series, load_series
from vortex.pipeline.segmentation import segment
from vortex.pipeline.meshing import generate_mesh
from vortex.pipeline.centerlines import compute_centerlines
from vortex.pipeline.flow_extensions import add_flow_extensions
from vortex.pipeline.exporter import export_stl
from vortex.pipeline.mesh_quality import check_mesh_quality, extract_bad_triangles

log = get_logger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Session State (for Shell mode)
# ---------------------------------------------------------------------------

class Session:
    def __init__(self):
        self.folder: Optional[str] = None
        self.series_uid: Optional[str] = None
        self.sitk_image: Any = None
        self.vtk_image: Any = None
        self.surface: Any = None
        self.centerlines: Any = None
        self.profiles: list = []
        self.final_surface: Any = None
        self.params: PipelineParams = PipelineParams()

session = Session()

# ---------------------------------------------------------------------------
# CLI Helpers
# ---------------------------------------------------------------------------

def display_welcome():
    logo = r"""[bold cyan]
        _.-'~`'-._
     .-'    _    '-.
    /    .-' '-.    \
   |    /  .-.  \    |
   |   |  (   )  |   |
   |    \  '-'  /    |
    \    '-._.-'    /
     '-.         .-'
        `'-...-'`

  _    ______  _____ _______ _______   __
 | |  / / __ \/ ___//_  __/ __/ |/ /  / /
 | | / / / / / /_    / / / __/|   /  / / 
 | |/ / /_/ / __/   / / / /_ /   |  /_/  
 |___/\____/_/     /_/ /___//_/|_| (_)   
[/bold cyan]"""
    console.print(Panel.fit(
        logo +
        "\n[bold blue]Vascular Output & Real-time Thresholding EXtraction[/bold blue]\n\n"
        "[dim]Advanced pipeline for CFD-ready aneurysm models.[/dim]",
        border_style="cyan"
    ))

def display_series_table(folder: str):
    with console.status(f"[bold green]Scanning {folder}..."):
        series = list_series(folder)
    
    if not series:
        console.print(f"[bold red]No DICOM series found in {folder}[/bold red]")
        return None

    table = Table(title=f"DICOM Series in {folder}", header_style="bold magenta")
    table.add_column("Index", justify="right", style="cyan")
    table.add_column("UID (Suffix)", style="dim")
    table.add_column("Slices", justify="right")
    table.add_column("Modality", justify="center")
    table.add_column("Description", style="green")

    for i, s in enumerate(series):
        uid_short = "..." + s['series_uid'][-12:]
        table.add_row(
            str(i+1),
            uid_short,
            str(s['num_slices']),
            s['modality'],
            s['description']
        )

    console.print(table)
    return series

def run_pipeline_step(step_name: str, func, *args, **kwargs):
    """Run a pipeline function with a live status spinner."""
    with console.status(f"[cyan]{step_name}...", spinner="dots") as status:
        def progress_cb(pct, msg):
            status.update(f"[cyan]{step_name}[/cyan] ─ [yellow]{pct}%[/yellow] ─ {msg}")
            
        result = func(*args, progress_cb=progress_cb, **kwargs)
        time.sleep(0.1)
    
    console.print(f"[bold green]✓[/bold green] {step_name} finished.")
    return result

def show_status_dashboard(session: Session):
    """Display a dashboard showing the current state of the pipeline."""
    table = Table(title="VORTEX Pipeline Status", box=box.ROUNDED, expand=True)
    table.add_column("Component", style="cyan", width=20)
    table.add_column("Status", style="bold")
    table.add_column("Details", style="dim")

    # DICOM
    if session.sitk_image is not None:
        table.add_row("DICOM Volume", "[green]✓ Loaded[/green]", f"Series: {session.series_uid}")
    else:
        table.add_row("DICOM Volume", "[red]✗ Missing[/red]", "Run 'load <dir>'")

    # Seed
    if session.params.seed_point_ijk is not None:
        table.add_row("Seed Point", "[green]✓ Picked[/green]", f"IJK: {session.params.seed_point_ijk}")
    else:
        table.add_row("Seed Point", "[yellow]⚠ Optional[/yellow]", "Run 'seed'")

    # Mask
    if session.vtk_image is not None:
        table.add_row("Segmentation Mask", "[green]✓ Extracted[/green]", "Run 'export-mask' to save")
    else:
        table.add_row("Segmentation Mask", "[red]✗ Missing[/red]", "Run 'segment'")

    # Surface
    if session.surface is not None:
        table.add_row("Surface Mesh", "[green]✓ Generated[/green]", "Run 'export' to save")
    else:
        table.add_row("Surface Mesh", "[red]✗ Missing[/red]", "Run 'mesh'")

    # Centerlines
    if session.centerlines is not None:
        table.add_row("Centerlines", "[green]✓ Computed[/green]", "Required for extensions")
    else:
        table.add_row("Centerlines", "[yellow]⚠ Optional[/yellow]", "Run 'centerlines'")

    # Capped Surface
    if session.final_surface is not None:
        table.add_row("Flow Extensions", "[green]✓ Applied[/green]", "Run 'export' to save CFD model")
    else:
        table.add_row("Flow Extensions", "[yellow]⚠ Optional[/yellow]", "Run 'extend'")

    console.print(table)

def display_quality_report(report: dict):
    """Render a mesh quality report dict as Rich tables."""
    s = report['stats']

    # ── Stats table ──────────────────────────────────────────────────────────
    stats_table = Table(title="Mesh Quality Report", box=box.ROUNDED, expand=True)
    stats_table.add_column("Check", style="cyan", width=26)
    stats_table.add_column("Result", style="bold")
    stats_table.add_column("Details", style="dim")

    # Basic stats
    stats_table.add_row("Points",        f"{s['points']:,}",    "")
    stats_table.add_row("Triangles",     f"{s['triangles']:,}", "")
    area = s['surface_area_mm2']
    stats_table.add_row("Surface Area",
        f"{area:.1f} mm²" if area else "N/A", "")
    bbox = s['bbox_mm']
    stats_table.add_row("Bounding Box",
        f"{bbox[0]} × {bbox[1]} × {bbox[2]} mm", "")

    # Non-manifold edges
    nm = report['non_manifold_edges']
    if nm == 0:
        stats_table.add_row("Non-manifold Edges",
            "[green]✓ None[/green]", "Manifold surface")
    else:
        stats_table.add_row("Non-manifold Edges",
            f"[red]✗ {nm}[/red]", "CFD mesher will fail")

    # Boundary loops
    loops = report['boundary_loops']
    if loops == 0:
        stats_table.add_row("Open Boundary Loops",
            "[green]✓ 0 (closed)[/green]", "Watertight")
    elif loops >= 2:
        stats_table.add_row("Open Boundary Loops",
            f"[yellow]⚠ {loops}[/yellow]", "Expected before 'extend'")
    else:
        stats_table.add_row("Open Boundary Loops",
            f"[red]✗ {loops}[/red]", "Need ≥2 for CFD")

    # Triangle quality
    q = report.get('triangle_quality')
    if q:
        ar_color = "green" if q['max_aspect_ratio'] <= 5.0 else (
                   "yellow" if q['max_aspect_ratio'] <= 20.0 else "red")
        stats_table.add_row("Aspect Ratio (mean/max)",
            f"[{ar_color}]{q['mean_aspect_ratio']} / {q['max_aspect_ratio']}[/{ar_color}]", "")
        if q.get('min_angle_deg') is not None:
            ang = q['min_angle_deg']
            ang_color = "green" if ang >= 10.0 else ("yellow" if ang >= 5.0 else "red")
            stats_table.add_row("Min Triangle Angle",
                f"[{ang_color}]{ang}°[/{ang_color}]", "")
    else:
        stats_table.add_row("Triangle Quality", "[dim]N/A[/dim]", "")

    # Normals
    n_flip  = report['normals_flipped']
    n_total = report['normals_total']
    if n_total == 0:
        stats_table.add_row("Normals", "[dim]N/A[/dim]", "No normals on mesh")
    elif n_flip == 0:
        stats_table.add_row("Normals",
            "[green]✓ Consistent[/green]", f"{n_total:,} points checked")
    else:
        frac = n_flip / n_total
        color = "yellow" if frac < 0.05 else "red"
        stats_table.add_row("Normals",
            f"[{color}]⚠ ~{n_flip:,} flipped[/{color}]",
            f"of {n_total:,} points")

    # Self-intersections
    si = report.get('self_intersections')
    if si is None:
        stats_table.add_row("Self-Intersections",
            "[dim]–[/dim]", "Run 'check --deep' to enable")
    elif si == 0:
        stats_table.add_row("Self-Intersections",
            "[green]✓ None[/green]", "")
    else:
        stats_table.add_row("Self-Intersections",
            f"[red]✗ {si}[/red]", "Fix in MeshLab")

    console.print(stats_table)

    # ── Issues summary ───────────────────────────────────────────────────────
    issues = report.get('issues', [])
    if issues:
        console.print()
        for severity, msg in issues:
            if severity == 'error':
                console.print(f"  [bold red]✗[/bold red] {msg}")
            elif severity == 'warning':
                console.print(f"  [bold yellow]⚠[/bold yellow] {msg}")
            else:
                console.print(f"  [bold blue]ℹ[/bold blue] {msg}")


def display_bad_triangles(worst, ar_threshold, export_path=None):
    """Print a Rich table of the worst triangles and report export status."""
    if not worst:
        console.print(f"  [green]✓ No triangles exceed AR {ar_threshold}[/green]")
        return

    t = Table(title=f"Worst Triangles (AR > {ar_threshold})", box=box.SIMPLE, expand=False)
    t.add_column("Rank",    style="dim",  width=5)
    t.add_column("AR",      style="red bold", width=10)
    t.add_column("Centroid X (mm)", width=16)
    t.add_column("Centroid Y (mm)", width=16)
    t.add_column("Centroid Z (mm)", width=16)

    for rank, (ar, (cx, cy, cz)) in enumerate(worst[:20], 1):
        t.add_row(str(rank), f"{ar:.2f}", f"{cx:.2f}", f"{cy:.2f}", f"{cz:.2f}")

    console.print(t)
    if len(worst) > 20:
        console.print(f"  [dim]… {len(worst) - 20} more bad triangles not shown[/dim]")

    if export_path:
        console.print(f"  [cyan]Bad triangles exported to:[/cyan] {export_path}")


def do_check_mesh(args):
    """CLI handler: check-mesh command."""
    from vortex.utils.vtk_compat import vtk as _vtk

    if not os.path.exists(args.input_stl):
        console.print(f"[bold red]File not found:[/bold red] {args.input_stl}")
        return 1

    console.print(f"\n[bold blue]Checking mesh:[/bold blue] {args.input_stl}")
    reader = _vtk.vtkSTLReader()
    reader.SetFileName(args.input_stl)
    reader.Update()
    mesh = reader.GetOutput()

    if mesh.GetNumberOfPoints() == 0:
        console.print("[bold red]STL loaded but contains no geometry.[/bold red]")
        return 1

    with console.status("[cyan]Running quality checks...", spinner="dots"):
        report = check_mesh_quality(mesh, deep=args.deep)

    display_quality_report(report)

    ar_threshold = getattr(args, 'ar_threshold', 20.0)
    export_bad   = getattr(args, 'export_bad', None)

    q = report.get('triangle_quality')
    if q and q['max_aspect_ratio'] > ar_threshold:
        console.print()
        with console.status("[cyan]Locating bad triangles...", spinner="dots"):
            bad_poly, worst = extract_bad_triangles(mesh, ar_threshold=ar_threshold)

        saved_path = None
        if bad_poly is not None and export_bad:
            from vortex.utils.vtk_compat import vtk as _vtk2
            writer = _vtk2.vtkSTLWriter()
            writer.SetInputData(bad_poly)
            writer.SetFileName(export_bad)
            writer.Write()
            saved_path = export_bad

        display_bad_triangles(worst, ar_threshold, export_path=saved_path)

    return 0


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

def do_list_series(args):
    display_series_table(args.folder)

def do_seed_picker(args):
    """Open the seed picker UI and return the resulting IJK."""
    from vortex.ui.seed_picker import pick_seed
    
    # Overwrite platform for the picker if it was forced to offscreen
    old_platform = os.environ.get("QT_QPA_PLATFORM")
    if old_platform == "offscreen":
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        if os.environ.get("LIBGL_ALWAYS_SOFTWARE") == "1":
            os.environ.pop("LIBGL_ALWAYS_SOFTWARE", None)

    console.print("\n[bold yellow]Opening Seed Picker window...[/bold yellow]")
    console.print("[dim]Workflow: 1. Click 'Seed' mode | 2. Click on aneurysm | 3. Confirm & Close[/dim]")
    
    ijk = pick_seed(args.folder, args.series_uid)
    
    # Restore platform
    if old_platform == "offscreen":
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    if ijk:
        ijk_str = f"{ijk[0]},{ijk[1]},{ijk[2]}"
        console.print(Panel(
            f"Successfully selected seed: [bold green]{ijk_str}[/bold green]\n\n"
            f"Run command:\n[bold cyan]./run-cli.sh process {args.folder} --seed-ijk {ijk_str} --roi-radius 50 ...[/bold cyan]",
            title="Seed Selection",
            border_style="green"
        ))
        return ijk
    else:
        console.print("\n[red]Seed picker closed without selection.[/red]")
        return None

def do_process_mesh(args):
    from vortex.utils.vtk_compat import vtk
    
    if not os.path.exists(args.input_stl):
        console.print(f"[bold red]Input STL file not found: {args.input_stl}[/bold red]")
        return 1

    params = PipelineParams(
        flow_ext_ratio=args.flow_ext_ratio,
        build_wall=(args.mode == "fsi"),
        wall_thickness=args.wall_thickness,
        solid=(args.mode == "solid"),
        split_patches=args.split_patches
    )

    console.print(f"\n[bold blue]Processing Mesh:[/bold blue] {args.input_stl}")
    
    reader = vtk.vtkSTLReader()
    reader.SetFileName(args.input_stl)
    reader.Update()
    surface = reader.GetOutput()
    
    if surface.GetNumberOfCells() == 0:
        console.print("[bold red]The input STL mesh is empty or invalid.[/bold red]")
        return 1

    centerlines, _ = run_pipeline_step("Centerlines", compute_centerlines, surface)
    final_surface = run_pipeline_step("Flow Extensions & Capping", add_flow_extensions, surface, centerlines, params)
    run_pipeline_step("Exporting STL", export_stl, final_surface, args.output, params)

    console.print(f"\n[bold green]Done![/bold green] Processed STL saved to: [cyan]{args.output}[/cyan]")
    return 0

def do_process(args):
    seed_ijk = None
    if args.seed_ijk:
        try:
            seed_ijk = tuple(map(int, args.seed_ijk.split(",")))
        except Exception:
            console.print(f"[bold red]Invalid seed-ijk format. Expected 'i,j,k', got '{args.seed_ijk}'[/bold red]")
            return 1

    params = PipelineParams(
        lower_threshold=args.lower_threshold,
        upper_threshold=args.upper_threshold,
        resample=args.resample,
        seed_point_ijk=seed_ijk,
        roi_radius=args.roi_radius,
        use_levelset=args.use_levelset,
        levelset_iterations=args.ls_iterations,
        levelset_curvature=args.ls_curvature,
        levelset_propagation=args.ls_propagation,
        reduce_mesh=args.reduce_mesh,
        increase_mesh=args.increase_mesh,
        flow_ext_ratio=args.flow_ext_ratio,
        build_wall=(args.mode == "fsi"),
        wall_thickness=args.wall_thickness,
        solid=(args.mode == "solid"),
        split_patches=args.split_patches,
    )

    console.print(f"\n[bold blue]Processing DICOM Pipeline:[/bold blue] {args.folder}")

    # 1. Load
    series = list_series(args.folder)
    if not series:
        return 1
    uid = args.series_uid or series[0]["series_uid"]
    sitk_image = load_series(args.folder, uid)

    # 2. Segment
    vtk_image = run_pipeline_step("Segmentation", segment, sitk_image, params)

    # 3. Mesh
    surface = run_pipeline_step("Meshing", generate_mesh, vtk_image, params)

    # Centerlines & Extensions
    final_surface = surface
    if args.centerlines:
        centerlines, _ = run_pipeline_step("Centerlines", compute_centerlines, surface)

        if args.flow_extensions:
            # Parse selective IDs if provided
            if args.flow_ext_ids:
                try:
                    params.flow_ext_selected = [int(i.strip()) for i in args.flow_ext_ids.split(",") if i.strip()]
                    console.print(f"[cyan]Selective extension IDs:[/cyan] {params.flow_ext_selected}")
                except ValueError:
                    console.print(f"[red]Invalid --flow-ext-ids format. Expected comma-separated integers, got '{args.flow_ext_ids}'[/red]")
                    return 1

            final_surface = run_pipeline_step("Flow Extensions", add_flow_extensions, surface, centerlines, params)

    
    # 5. Export
    run_pipeline_step("Exporting STL", export_stl, final_surface, args.output, params)

    console.print(f"\n[bold green]Pipeline Complete![/bold green] Saved to: [cyan]{args.output}[/cyan]")
    return 0


# ---------------------------------------------------------------------------
# Shell Mode (Interactive)
# ---------------------------------------------------------------------------

def do_shell():
    display_welcome()
    show_status_dashboard(session)
    console.print("[dim]Type 'help' for commands, 'exit' to quit.[/dim]\n")

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    import shlex

    commands = ["load", "load-mesh", "list", "seed", "segment", "mesh", "check", "centerlines", "extend", "export", "export-mask", "status", "params", "metrics", "help", "exit"]
    completer = WordCompleter(commands, ignore_case=True)
    ps = PromptSession(completer=completer)

    while True:
        try:
            text = ps.prompt("vortex> ").strip()
            if not text: continue

            try:
                parts = shlex.split(text)
            except ValueError as e:
                console.print(f"[red]Error parsing command:[/red] {e}")
                continue

            if not parts: continue
            cmd = parts[0].lower()

            if cmd == "exit":
                break

            elif cmd == "help":
                console.print(
                    "[bold]Available Commands:[/bold]\n"
                    "  [cyan]load <dir>[/cyan]         Load DICOM folder\n"
                    "  [cyan]load-mesh <file>[/cyan]  Load external STL as surface (skips segment/mesh)\n"
                    "  [cyan]list[/cyan]               List series in loaded folder\n"
                    "  [cyan]seed[/cyan]           Open visual seed picker\n"
                    "  [cyan]status[/cyan]         Show pipeline dashboard\n"
                    "  [cyan]segment[/cyan]        Run segmentation\n"
                    "  [cyan]mesh[/cyan]           Generate mesh\n"
                    "  [cyan]centerlines[/cyan]    Compute centerlines\n"
                    "  [cyan]extend[/cyan]         Add flow extensions & cap\n"
                    "  [cyan]check[/cyan]                        Check mesh quality (manifold, holes, triangle quality)\n"
                    "  [cyan]check --deep[/cyan]                 Also run self-intersection detection (slow)\n"
                    "  [cyan]check --export-bad bad.stl[/cyan]   Export bad triangles (AR>20) to STL\n"
                    "  [cyan]check --ar-threshold 10[/cyan]      Change bad-triangle AR threshold\n"
                    "  [cyan]export <file>[/cyan]   Export final STL\n"
                    "  [cyan]export-mask <f>[/cyan] Export segmentation mask (NIfTI)\n"
                    "  [cyan]metrics[/cyan]        Compute aneurysm metrics\n"
                    "  [cyan]params[/cyan]         Show/edit parameters\n"
                    "  [cyan]exit[/cyan]           Quit shell"
                )

            elif cmd == "status":
                show_status_dashboard(session)

            elif cmd == "load":
                if len(parts) < 2:
                    console.print("[red]Usage: load <folder_path>[/red]")
                    continue
                session.folder = parts[1]
                series = display_series_table(session.folder)
                if series:
                    idx = Prompt.ask("Select series index", default="1")
                    try:
                        session.series_uid = series[int(idx)-1]["series_uid"]
                        session.sitk_image = load_series(session.folder, session.series_uid)
                        console.print(f"[green]Loaded series:[/green] {session.series_uid[-8:]}")
                    except Exception as e:
                        console.print(f"[red]Error loading:[/red] {e}")

            elif cmd == "load-mesh":
                if len(parts) < 2:
                    console.print("[red]Usage: load-mesh <path/to/file.stl>[/red]")
                    continue
                stl_path = parts[1]
                if not os.path.isfile(stl_path):
                    console.print(f"[red]File not found:[/red] {stl_path}")
                    continue
                try:
                    from vortex.utils.vtk_compat import vtk
                    reader = vtk.vtkSTLReader()
                    reader.SetFileName(stl_path)
                    reader.Update()
                    loaded = reader.GetOutput()
                    if loaded.GetNumberOfPoints() == 0:
                        console.print("[red]STL loaded but contains no geometry.[/red]")
                        continue
                    # Replace surface; clear downstream results that are now stale
                    session.surface       = loaded
                    session.final_surface = loaded
                    session.centerlines   = None
                    session.profiles      = None
                    n_pts   = loaded.GetNumberOfPoints()
                    n_cells = loaded.GetNumberOfCells()
                    console.print(
                        f"[green]Loaded:[/green] {stl_path}\n"
                        f"  {n_pts:,} points, {n_cells:,} triangles\n"
                        f"[dim]Ready for 'centerlines' → 'extend' → 'export'[/dim]"
                    )
                except Exception as e:
                    console.print(f"[red]Failed to load STL:[/red] {e}")

            elif cmd == "list":
                if not session.folder:
                    console.print("[red]No folder loaded. Use 'load <dir>'.[/red]")
                else:
                    display_series_table(session.folder)

            elif cmd == "seed":
                if not session.folder:
                    console.print("[red]No folder loaded.[/red]")
                else:
                    # Mock args for seed_picker
                    class Args: pass
                    args = Args()
                    args.folder = session.folder
                    args.series_uid = session.series_uid
                    ijk = do_seed_picker(args)
                    if ijk:
                        session.params.seed_point_ijk = ijk

            elif cmd == "segment":
                if session.sitk_image is None:
                    console.print("[red]No image loaded.[/red]")
                else:
                    session.vtk_image = run_pipeline_step("Segmentation", segment, session.sitk_image, session.params)

            elif cmd == "mesh":
                if session.vtk_image is None:
                    console.print("[red]Run 'segment' first.[/red]")
                else:
                    session.surface = run_pipeline_step("Meshing", generate_mesh, session.vtk_image, session.params)
                    session.final_surface = session.surface

            elif cmd == "check":
                target = session.final_surface or session.surface
                if target is None:
                    console.print("[red]No mesh loaded. Run 'mesh' or 'load-mesh' first.[/red]")
                else:
                    deep = "--deep" in parts

                    # parse --ar-threshold N
                    ar_threshold = 20.0
                    if "--ar-threshold" in parts:
                        idx = parts.index("--ar-threshold")
                        try:
                            ar_threshold = float(parts[idx + 1])
                        except (IndexError, ValueError):
                            console.print("[yellow]--ar-threshold requires a number, using 20.0[/yellow]")

                    # parse --export-bad <file>
                    export_bad = None
                    if "--export-bad" in parts:
                        idx = parts.index("--export-bad")
                        try:
                            export_bad = parts[idx + 1]
                        except IndexError:
                            console.print("[yellow]--export-bad requires a file path[/yellow]")

                    if deep:
                        console.print("[dim]Running deep check (self-intersections — may be slow)...[/dim]")
                    with console.status("[cyan]Running quality checks...", spinner="dots"):
                        report = check_mesh_quality(target, deep=deep)
                    display_quality_report(report)

                    q = report.get('triangle_quality')
                    if q and q['max_aspect_ratio'] > ar_threshold:
                        console.print()
                        with console.status("[cyan]Locating bad triangles...", spinner="dots"):
                            bad_poly, worst = extract_bad_triangles(target, ar_threshold=ar_threshold)

                        saved_path = None
                        if bad_poly is not None and export_bad:
                            from vortex.utils.vtk_compat import vtk as _vtk2
                            writer = _vtk2.vtkSTLWriter()
                            writer.SetInputData(bad_poly)
                            writer.SetFileName(export_bad)
                            writer.Write()
                            saved_path = export_bad

                        display_bad_triangles(worst, ar_threshold, export_path=saved_path)

            elif cmd == "centerlines":
                if session.surface is None:
                    console.print("[red]Run 'mesh' first.[/red]")
                else:
                    session.centerlines, session.profiles = run_pipeline_step("Centerlines", compute_centerlines, session.surface)
                    
                    if session.profiles:
                        table = Table(title="Detected Vessel Boundaries", header_style="bold magenta")
                        table.add_column("ID", justify="right", style="cyan")
                        table.add_column("Location (x,y,z)", style="green")
                        table.add_column("Radius (mm)", justify="right", style="yellow")
                        
                        for p in session.profiles:
                            loc = f"{p['center_mm'][0]:.1f}, {p['center_mm'][1]:.1f}, {p['center_mm'][2]:.1f}"
                            table.add_row(str(p['id']), loc, str(p['radius_mm']))
                        
                        console.print(table)
                        console.print("[dim]Use these IDs to select specific vessels for 'extend' (Future). Currently all are extended.[/dim]")

            elif cmd == "extend":
                if session.centerlines is None:
                    console.print("[red]Run 'centerlines' first.[/red]")
                else:
                    # Allow optional ID selection
                    ids = None
                    if len(parts) > 1:
                        try:
                            # Support both 'extend 1,2,3' and 'extend 1 2 3'
                            if "," in parts[1]:
                                ids = [int(i.strip()) for i in parts[1].split(",") if i.strip()]
                            else:
                                ids = [int(i) for i in parts[1:]]
                            console.print(f"[cyan]Selected vessel IDs for extension:[/cyan] {ids}")
                        except ValueError:
                            console.print(f"[red]Invalid ID format. Expected integers, got '{parts[1:]}'[/red]")
                            continue
                    
                    session.params.flow_ext_selected = ids
                    session.final_surface = run_pipeline_step("Flow Extensions", add_flow_extensions, session.surface, session.centerlines, session.params)

            elif cmd == "metrics":
                if session.surface is None:
                    console.print("[red]Run 'mesh' first to generate a surface.[/red]")
                elif not session.params.seed_point_ijk:
                    console.print("[red]A seed point is required to estimate aneurysm metrics. Run 'seed' or set 'seed_ijk' in params.[/red]")
                else:
                    from vortex.pipeline.measurement import estimate_aneurysm_geometry
                    from vortex.pipeline.dicom_loader import ijk_to_mm
                    
                    if session.sitk_image:
                        seed_mm = ijk_to_mm(session.sitk_image, session.params.seed_point_ijk)
                        
                        with console.status("[bold green]Calculating metrics..."):
                            metrics = estimate_aneurysm_geometry(session.surface, seed_mm)
                        
                        if metrics:
                            table = Table(title="Morphological Metrics", header_style="bold magenta")
                            table.add_column("Metric", style="cyan")
                            table.add_column("Value", justify="right", style="green")
                            
                            for k, v in metrics.items():
                                table.add_row(k.replace("_", " ").title(), str(v))
                            
                            console.print(table)
                        else:
                            console.print("[red]Failed to compute metrics. Ensure the seed is near the aneurysm on the surface.[/red]")
                    else:
                         console.print("[red]DICOM image not loaded. Cannot convert seed IJK to physical mm.[/red]")

            elif cmd == "export":
                if session.final_surface is None:
                    console.print("[red]Nothing to export.[/red]")
                else:
                    path = parts[1] if len(parts) > 1 else "output.stl"
                    run_pipeline_step("Exporting STL", export_stl, session.final_surface, path, session.params)

            elif cmd == "export-mask":
                if session.vtk_image is None:
                    console.print("[red]Run 'segment' first to generate a mask.[/red]")
                else:
                    path = parts[1] if len(parts) > 1 else "segmentation_mask.nii.gz"
                    
                    def _export_mask_step(vtk_img, p, cb=None):
                        import SimpleITK as sitk
                        import vtk
                        
                        if cb: cb(10, "Converting VTK image to SimpleITK...")
                        
                        # Convert vtkImageData to sitk.Image
                        from vortex.utils.vtk_compat import vtk_to_numpy
                        array = vtk_to_numpy(vtk_img)
                        sitk_mask = sitk.GetImageFromArray(array)
                        
                        if session.sitk_image:
                            sitk_mask.SetSpacing(session.sitk_image.GetSpacing())
                            sitk_mask.SetOrigin(session.sitk_image.GetOrigin())
                            sitk_mask.SetDirection(session.sitk_image.GetDirection())
                            
                        if cb: cb(60, f"Writing {path}...")
                        sitk.WriteImage(sitk_mask, path)
                        if cb: cb(100, "Done.")
                        
                    run_pipeline_step("Exporting Mask (NIfTI)", _export_mask_step, session.vtk_image, path)
                    console.print(f"[bold green]Saved mask to:[/bold green] {path}")

            elif cmd == "params":
                table = Table(title="Pipeline Parameters")
                table.add_column("Param", style="cyan")
                table.add_column("Value", style="green")
                
                p = session.params
                table.add_row("lower_threshold", str(p.lower_threshold))
                table.add_row("upper_threshold", str(p.upper_threshold))
                table.add_row("resample", str(p.resample))
                table.add_row("seed_ijk", str(p.seed_point_ijk))
                table.add_row("roi_radius", str(p.roi_radius))
                table.add_row("use_levelset", str(p.use_levelset))
                table.add_row("levelset_iterations", str(p.levelset_iterations))
                table.add_row("levelset_curvature", str(p.levelset_curvature))
                table.add_row("levelset_propagation", str(p.levelset_propagation))
                table.add_row("flow_ext_ratio", str(p.flow_ext_ratio))
                table.add_row("output_mode", "fsi" if p.build_wall else "solid" if p.solid else "cfd")
                table.add_row("split_patches", str(p.split_patches))
                
                console.print(table)
                if Confirm.ask("Edit a parameter?"):
                    key = Prompt.ask("Parameter name", choices=["lower", "upper", "resample", "roi", "levelset", "ls_iter", "ls_curv", "ls_prop", "ratio", "split"])
                    val = Prompt.ask("New value")
                    if key == "lower": p.lower_threshold = float(val)
                    if key == "upper": p.upper_threshold = float(val)
                    if key == "resample": p.resample = float(val)
                    if key == "roi": p.roi_radius = float(val)
                    if key == "levelset": p.use_levelset = (val.lower() == "true")
                    if key == "ls_iter": p.levelset_iterations = int(val)
                    if key == "ls_curv": p.levelset_curvature = float(val)
                    if key == "ls_prop": p.levelset_propagation = float(val)
                    if key == "ratio": p.flow_ext_ratio = float(val)
                    if key == "split": p.split_patches = (val.lower() == "true")

            else:
                console.print(f"[red]Unknown command:[/red] {cmd}")

        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        except Exception as e:
            console.print(f"[bold red]Error:[/bold red] {e}")

    console.print("\n[blue]Exiting VORTEX Shell.[/blue]")

# ---------------------------------------------------------------------------
# Main Entry
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VORTEX Aneurysm — CFD-ready 3D model generator from DICOM images."
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: shell
    subparsers.add_parser("shell", help="Enter interactive shell mode")

    # Command: list-series
    list_p = subparsers.add_parser("list-series", help="List DICOM series in a folder")
    list_p.add_argument("folder", help="Path to DICOM folder")

    # Command: seed-picker
    seed_p = subparsers.add_parser("seed-picker", help="Open a visual window to pick a seed point")
    seed_p.add_argument("folder", help="Path to DICOM folder")
    seed_p.add_argument("--series-uid", help="Specific SeriesInstanceUID to load (default: largest)")

    # Command: check-mesh
    chk_p = subparsers.add_parser("check-mesh", help="Report mesh quality (manifold, holes, triangle quality)")
    chk_p.add_argument("input_stl", help="Path to STL file to check")
    chk_p.add_argument("--deep", action="store_true",
                       help="Also run self-intersection detection (slow)")
    chk_p.add_argument("--export-bad", metavar="FILE",
                       help="Export bad triangles (above --ar-threshold) to this STL file")
    chk_p.add_argument("--ar-threshold", type=float, default=20.0, metavar="N",
                       help="Aspect-ratio threshold for bad-triangle detection (default: 20.0)")

    # Command: process-mesh
    mesh_p = subparsers.add_parser("process-mesh", help="Apply centerlines/extensions/capping to an existing STL")
    mesh_p.add_argument("input_stl", help="Path to input STL file")
    mesh_p.add_argument("--output", "-o", default="processed_mesh.stl", help="Path to output STL file")
    mesh_p.add_argument("--flow-ext-ratio", type=float, default=5.0, help="Extension length ratio (default: 5.0)")
    mesh_p.add_argument("--mode", choices=["cfd", "fsi", "solid"], default="cfd", help="STL output mode (default: cfd)")
    mesh_p.add_argument("--wall-thickness", type=float, default=0.2, help="Wall thickness for FSI mode (default: 0.2)")
    mesh_p.add_argument("--split-patches", action="store_true", help="Split CFD output into separate STLs for wall and caps")

    # Command: process
    proc_p = subparsers.add_parser("process", help="Run the full pipeline")
    proc_p.add_argument("folder", help="Path to DICOM folder")
    proc_p.add_argument("--series-uid", help="Specific SeriesInstanceUID to load (default: largest)")
    proc_p.add_argument("--output", "-o", default="output.stl", help="Path to output STL file (default: output.stl)")

    # Segmentation
    seg_g = proc_p.add_argument_group("Segmentation Parameters")
    seg_g.add_argument("--lower-threshold", type=float, default=150.0, help="Lower HU threshold")
    seg_g.add_argument("--upper-threshold", type=float, default=400.0, help="Upper HU threshold")
    seg_g.add_argument("--resample", type=float, default=2.0, help="Isotropic resample factor")
    seg_g.add_argument("--seed-ijk", help="Seed point coordinates as 'i,j,k' (optional)")
    seg_g.add_argument("--roi-radius", type=float, default=0.0, help="ROI radius in mm around seed")

    # Level-set
    ls_g = proc_p.add_argument_group("Level-set Parameters")
    ls_g.add_argument("--use-levelset", action="store_true", help="Enable level-set refinement")
    ls_g.add_argument("--ls-iterations", type=int, default=500, help="Max level-set iterations")
    ls_g.add_argument("--ls-curvature", type=float, default=0.5, help="Level-set curvature scaling")
    ls_g.add_argument("--ls-propagation", type=float, default=1.0, help="Level-set propagation scaling")

    # Mesh
    mesh_g = proc_p.add_argument_group("Mesh Quality Parameters")
    mesh_g.add_argument("--reduce-mesh", type=float, default=0.0, help="Mesh reduction fraction")
    mesh_g.add_argument("--increase-mesh", type=int, default=0, help="Mesh subdivision passes")

    # Flow extensions
    flow_g = proc_p.add_argument_group("Flow Extensions")
    flow_g.add_argument("--centerlines", action="store_true", help="Compute vessel centerlines")
    flow_g.add_argument("--flow-extensions", action="store_true", help="Add flow extensions")
    flow_g.add_argument("--flow-ext-ratio", type=float, default=5.0, help="Extension length ratio")
    flow_g.add_argument("--flow-ext-ids", help="Comma-separated list of boundary IDs to extend (optional)")

    # Output mode
    out_g = proc_p.add_argument_group("Output Mode")
    out_g.add_argument("--mode", choices=["cfd", "fsi", "solid"], default="cfd", help="STL output mode")
    out_g.add_argument("--wall-thickness", type=float, default=0.2, help="Wall thickness for FSI mode")
    out_g.add_argument("--split-patches", action="store_true", help="Split CFD output into separate STLs for wall and caps")

    return parser

def main():
    setup_logging()
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        if args.command == "shell":
            do_shell()
        elif args.command == "list-series":
            do_list_series(args)
        elif args.command == "seed-picker":
            do_seed_picker(args)
        elif args.command == "check-mesh":
            sys.exit(do_check_mesh(args))
        elif args.command == "process-mesh":
            sys.exit(do_process_mesh(args))
        elif args.command == "process":
            sys.exit(do_process(args))
    except Exception as e:
        console.print_exception(show_locals=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
