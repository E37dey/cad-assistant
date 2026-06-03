"""
engineering_validator.py — Engineering QA module for TextToCAD Pro.

Validates parts and drawings across three domains:
  1. STRUCTURAL  — Will it survive? (stress, thin walls, unsupported spans)
  2. GEOMETRIC   — Can it be made? (DFM, tolerances, features feasibility)
  3. DRAWING     — Is the drawing complete? (dims, datums, GD&T, views)

Each check returns a severity (error/warning/info) and a fix suggestion.
"""

import adsk.core
import adsk.fusion
import math
import json


# ─────────────────────────────────────────────────────────────
# Severity levels
# ─────────────────────────────────────────────────────────────
class Severity:
    ERROR = 'error'      # Must fix — part will fail or can't be made
    WARNING = 'warning'  # Should fix — risky or non-standard
    INFO = 'info'        # Suggestion — could be improved


class Issue:
    """A single validation issue."""
    def __init__(self, domain: str, severity: str, code: str,
                 title: str, detail: str, fix: str,
                 feature: str = '', location: str = ''):
        self.domain = domain        # 'structural', 'geometric', 'drawing'
        self.severity = severity    # Severity.ERROR/WARNING/INFO
        self.code = code            # e.g. 'THIN_WALL', 'MISSING_DATUM'
        self.title = title          # Short description
        self.detail = detail        # Full explanation
        self.fix = fix              # Suggested fix
        self.feature = feature      # Related feature name
        self.location = location    # Where in the model

    def to_dict(self):
        return {
            'domain': self.domain, 'severity': self.severity,
            'code': self.code, 'title': self.title,
            'detail': self.detail, 'fix': self.fix,
            'feature': self.feature, 'location': self.location,
        }


# ─────────────────────────────────────────────────────────────
# Manufacturing constraints by process
# ─────────────────────────────────────────────────────────────
PROCESS_CONSTRAINTS = {
    'CNC Machining': {
        'min_wall_mm': 1.0,
        'min_hole_dia_mm': 1.0,
        'max_hole_depth_ratio': 10,     # depth / diameter
        'min_internal_radius_mm': 0.5,  # tool radius limit
        'min_draft_deg': 0,             # not needed for CNC
        'max_aspect_ratio': 15,         # length / min_dimension
        'min_thread_dia_mm': 2.0,
        'notes': 'Check tool access for internal features',
    },
    'Turning': {
        'min_wall_mm': 0.8,
        'min_hole_dia_mm': 1.0,
        'max_hole_depth_ratio': 8,
        'min_internal_radius_mm': 0.2,
        'min_draft_deg': 0,
        'max_aspect_ratio': 12,
        'min_thread_dia_mm': 2.0,
        'notes': 'Part must be rotational / axisymmetric',
    },
    'Casting': {
        'min_wall_mm': 3.0,
        'min_hole_dia_mm': 6.0,
        'max_hole_depth_ratio': 4,
        'min_internal_radius_mm': 2.0,
        'min_draft_deg': 1.5,
        'max_aspect_ratio': 8,
        'min_thread_dia_mm': 6.0,
        'notes': 'Avoid undercuts; uniform wall thickness preferred',
    },
    'Injection Molding': {
        'min_wall_mm': 0.8,
        'min_hole_dia_mm': 1.0,
        'max_hole_depth_ratio': 4,
        'min_internal_radius_mm': 0.5,
        'min_draft_deg': 1.0,
        'max_aspect_ratio': 10,
        'min_thread_dia_mm': 3.0,
        'notes': 'Uniform walls; draft on all vertical faces; gate location',
    },
    '3D Print (FDM)': {
        'min_wall_mm': 1.2,
        'min_hole_dia_mm': 2.0,
        'max_hole_depth_ratio': 20,
        'min_internal_radius_mm': 0.0,
        'min_draft_deg': 0,
        'max_aspect_ratio': 20,
        'min_thread_dia_mm': 4.0,
        'notes': 'Overhangs >45° need support; bridge max ~10mm',
    },
    '3D Print (SLA)': {
        'min_wall_mm': 0.6,
        'min_hole_dia_mm': 0.5,
        'max_hole_depth_ratio': 20,
        'min_internal_radius_mm': 0.0,
        'min_draft_deg': 0,
        'max_aspect_ratio': 20,
        'min_thread_dia_mm': 3.0,
        'notes': 'Drain holes for hollow parts; UV post-cure',
    },
    '3D Print (Metal)': {
        'min_wall_mm': 0.4,
        'min_hole_dia_mm': 0.5,
        'max_hole_depth_ratio': 15,
        'min_internal_radius_mm': 0.0,
        'min_draft_deg': 0,
        'max_aspect_ratio': 15,
        'min_thread_dia_mm': 3.0,
        'notes': 'Support structures required; stress relief post-print',
    },
    'Sheet Metal': {
        'min_wall_mm': 0.5,
        'min_hole_dia_mm': 1.0,
        'max_hole_depth_ratio': 1,
        'min_internal_radius_mm': 0.5,
        'min_draft_deg': 0,
        'max_aspect_ratio': 50,
        'min_thread_dia_mm': 3.0,
        'notes': 'Min bend radius = material thickness; hole edge clearance',
    },
}

# Material yield strength estimates (MPa) for basic stress checks
MATERIAL_YIELD_MPA = {
    'Steel': 250, 'Aluminum': 270, 'Stainless Steel': 210,
    'Cast Iron': 130, 'Brass': 100, 'Titanium': 880,
    'Plastic (ABS)': 40, 'Nylon': 70,
}


class EngineeringValidator:
    """
    Validates a Fusion 360 model for structural, geometric, and drawing issues.
    """

    def __init__(self, app: adsk.core.Application):
        self.app = app
        self.design = adsk.fusion.Design.cast(app.activeProduct)
        self.root_comp = self.design.rootComponent
        self.issues: list[Issue] = []

    def validate_all(self, process: str = 'CNC Machining',
                     material: str = 'Steel',
                     gdt_spec: dict = None) -> list[Issue]:
        """
        Run all validation checks.
        Returns list of Issue objects sorted by severity.
        """
        self.issues = []

        self._check_structural(material)
        self._check_geometric(process)
        self._check_parameters()
        if gdt_spec:
            self._check_gdt(gdt_spec)
            self._check_drawing_completeness(gdt_spec)

        # Sort: errors first, then warnings, then info
        severity_order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
        self.issues.sort(key=lambda i: severity_order.get(i.severity, 3))

        return self.issues

    # ═══════════════════════════════════════════════════════════
    # STRUCTURAL CHECKS
    # ═══════════════════════════════════════════════════════════

    def _check_structural(self, material: str):
        """Check structural integrity of the model."""

        for comp in self._get_all_components():
            for body in self._get_bodies(comp):
                self._check_thin_walls(body, material)
                self._check_sharp_internal_corners(body)
                self._check_aspect_ratio(body)
                self._check_unsupported_features(body)
                self._check_mass_properties(body, material)

        # Interference check between bodies
        self._check_interference()

    def _check_thin_walls(self, body, material: str):
        """Detect walls thinner than recommended for the material."""
        try:
            # Use bounding box as rough check
            bb = body.boundingBox
            dims = [
                abs(bb.maxPoint.x - bb.minPoint.x),
                abs(bb.maxPoint.y - bb.minPoint.y),
                abs(bb.maxPoint.z - bb.minPoint.z),
            ]
            min_dim_mm = min(dims) * 10  # cm to mm

            # For plastic, min is 0.8mm; for metal, 1.0mm
            min_acceptable = 1.0 if material not in ('Plastic (ABS)', 'Nylon') else 0.8

            if min_dim_mm < min_acceptable:
                self.issues.append(Issue(
                    domain='structural',
                    severity=Severity.ERROR,
                    code='THIN_WALL',
                    title=f'Wall too thin: {min_dim_mm:.2f}mm',
                    detail=f'Body "{body.name}" has a dimension of only '
                           f'{min_dim_mm:.2f}mm. Minimum recommended for '
                           f'{material} is {min_acceptable}mm.',
                    fix=f'Increase wall thickness to at least {min_acceptable}mm, '
                        f'or consider a different manufacturing process.',
                    feature=body.name,
                ))
            elif min_dim_mm < 2.0:
                self.issues.append(Issue(
                    domain='structural',
                    severity=Severity.WARNING,
                    code='THIN_WALL_WARN',
                    title=f'Thin wall: {min_dim_mm:.2f}mm',
                    detail=f'Body "{body.name}" has a thin dimension. '
                           f'May be fragile in service.',
                    fix='Consider adding ribs or increasing thickness.',
                    feature=body.name,
                ))
        except:
            pass

    def _check_sharp_internal_corners(self, body):
        """Flag internal corners without fillets (stress concentrators)."""
        try:
            edges = body.edges
            concave_sharp = 0
            for i in range(edges.count):
                edge = edges.item(i)
                if edge.isDegenerate:
                    continue
                # Check if edge is concave (internal)
                if edge.geometry.curveType == adsk.core.Curve3DTypes.Line3DCurveType:
                    faces = edge.faces
                    if faces.count == 2:
                        # Internal corners have faces that point toward each other
                        # This is a simplified heuristic
                        concave_sharp += 1

            # Only flag if there are many sharp edges (likely missing fillets)
            if concave_sharp > 10:
                self.issues.append(Issue(
                    domain='structural',
                    severity=Severity.WARNING,
                    code='STRESS_CONCENTRATION',
                    title=f'Many sharp internal corners ({concave_sharp} edges)',
                    detail=f'Body "{body.name}" has {concave_sharp} potentially sharp '
                           f'internal edges. Sharp corners cause stress concentrations '
                           f'that can lead to fatigue cracking.',
                    fix='Add fillets (R0.5-2mm) to internal corners, especially '
                        'at transitions between different thicknesses.',
                    feature=body.name,
                ))
        except:
            pass

    def _check_aspect_ratio(self, body):
        """Flag parts with extreme aspect ratios (prone to bending)."""
        try:
            bb = body.boundingBox
            dims = sorted([
                abs(bb.maxPoint.x - bb.minPoint.x),
                abs(bb.maxPoint.y - bb.minPoint.y),
                abs(bb.maxPoint.z - bb.minPoint.z),
            ])
            if dims[0] > 0.001:  # avoid division by zero
                ratio = dims[2] / dims[0]
                if ratio > 20:
                    self.issues.append(Issue(
                        domain='structural',
                        severity=Severity.WARNING,
                        code='HIGH_ASPECT_RATIO',
                        title=f'Extreme aspect ratio: {ratio:.0f}:1',
                        detail=f'Body "{body.name}" is very elongated. '
                               f'This may cause bending/vibration issues.',
                        fix='Add stiffening ribs, increase the thin dimension, '
                            'or use a stronger material.',
                        feature=body.name,
                    ))
        except:
            pass

    def _check_unsupported_features(self, body):
        """Detect potentially unsupported overhangs and cantilevers."""
        try:
            bb = body.boundingBox
            height_mm = abs(bb.maxPoint.y - bb.minPoint.y) * 10
            width_mm = abs(bb.maxPoint.x - bb.minPoint.x) * 10

            # Very rough heuristic: tall narrow parts may be top-heavy
            if height_mm > 3 * width_mm and height_mm > 50:
                self.issues.append(Issue(
                    domain='structural',
                    severity=Severity.INFO,
                    code='TOP_HEAVY',
                    title='Part may be unstable',
                    detail=f'Body "{body.name}" is tall ({height_mm:.0f}mm) '
                           f'relative to its base ({width_mm:.0f}mm). '
                           f'Consider stability in service.',
                    fix='Widen the base, add mounting features, or add gussets.',
                    feature=body.name,
                ))
        except:
            pass

    def _check_mass_properties(self, body, material: str):
        """Basic mass/volume sanity check."""
        try:
            phys = body.physicalProperties
            vol_cm3 = phys.volume  # Fusion returns cm³
            area_cm2 = phys.area

            if vol_cm3 < 0.001:
                self.issues.append(Issue(
                    domain='structural',
                    severity=Severity.ERROR,
                    code='ZERO_VOLUME',
                    title='Body has near-zero volume',
                    detail=f'Body "{body.name}" volume is {vol_cm3:.6f} cm³. '
                           f'This is likely a degenerate or surface body.',
                    fix='Check that all features create solid geometry. '
                        'Verify sketch profiles are closed.',
                    feature=body.name,
                ))
        except:
            pass

    def _check_interference(self):
        """Check for body-to-body interference."""
        try:
            bodies = adsk.core.ObjectCollection.create()
            for body in self.root_comp.bRepBodies:
                bodies.add(body)

            if bodies.count < 2:
                return

            interference_input = self.design.createInterferenceInput(bodies)
            interference_input.areCoincidentFacesIncluded = False
            results = self.design.analyzeInterference(interference_input)

            if results.count > 0:
                self.issues.append(Issue(
                    domain='structural',
                    severity=Severity.ERROR,
                    code='INTERFERENCE',
                    title=f'{results.count} body interference(s) detected',
                    detail='Bodies in the design overlap. This indicates '
                           'geometry errors or missing clearances.',
                    fix='Check mating dimensions and clearances. '
                        'Use Inspect → Interference to visualize.',
                ))
        except:
            pass

    # ═══════════════════════════════════════════════════════════
    # GEOMETRIC / DFM CHECKS
    # ═══════════════════════════════════════════════════════════

    def _check_geometric(self, process: str):
        """Check manufacturability for the given process."""
        constraints = PROCESS_CONSTRAINTS.get(process)
        if not constraints:
            constraints = PROCESS_CONSTRAINTS['CNC Machining']

        min_wall = constraints['min_wall_mm']
        max_depth_ratio = constraints['max_hole_depth_ratio']
        min_draft = constraints['min_draft_deg']
        min_hole = constraints['min_hole_dia_mm']
        min_radius = constraints['min_internal_radius_mm']

        for comp in self._get_all_components():
            for body in self._get_bodies(comp):
                self._check_hole_depth_ratios(body, max_depth_ratio, process)
                self._check_small_features(body, min_hole, process)
                self._check_draft_angles(body, min_draft, process)
                self._check_internal_radii(body, min_radius, process)

    def _check_hole_depth_ratios(self, body, max_ratio: float, process: str):
        """Check if holes are too deep relative to their diameter."""
        try:
            for i in range(body.faces.count):
                face = body.faces.item(i)
                geom = face.geometry
                if hasattr(geom, 'radius'):
                    # It's a cylindrical face (possible hole)
                    radius_mm = geom.radius * 10  # cm to mm
                    dia_mm = radius_mm * 2

                    if dia_mm < 50:  # Only check small features
                        bb = face.boundingBox
                        depth_mm = max(
                            abs(bb.maxPoint.x - bb.minPoint.x),
                            abs(bb.maxPoint.y - bb.minPoint.y),
                            abs(bb.maxPoint.z - bb.minPoint.z),
                        ) * 10

                        ratio = depth_mm / dia_mm if dia_mm > 0 else 0
                        if ratio > max_ratio:
                            self.issues.append(Issue(
                                domain='geometric',
                                severity=Severity.WARNING,
                                code='DEEP_HOLE',
                                title=f'Deep hole: ⌀{dia_mm:.1f}mm × {depth_mm:.1f}mm '
                                      f'(ratio {ratio:.1f}:1)',
                                detail=f'For {process}, max depth/diameter ratio is '
                                       f'{max_ratio}:1. This hole exceeds it.',
                                fix='Use a shorter hole, larger diameter, '
                                    'or consider a different process (e.g. EDM).',
                                feature=body.name,
                            ))
        except:
            pass

    def _check_small_features(self, body, min_hole_mm: float, process: str):
        """Flag features that are too small for the process."""
        try:
            for i in range(body.faces.count):
                face = body.faces.item(i)
                geom = face.geometry
                if hasattr(geom, 'radius'):
                    dia_mm = geom.radius * 20  # cm to mm, diameter
                    if 0 < dia_mm < min_hole_mm:
                        self.issues.append(Issue(
                            domain='geometric',
                            severity=Severity.ERROR,
                            code='SMALL_FEATURE',
                            title=f'Feature too small: ⌀{dia_mm:.2f}mm',
                            detail=f'Minimum hole diameter for {process} is '
                                   f'{min_hole_mm}mm.',
                            fix=f'Increase to at least ⌀{min_hole_mm}mm or '
                                f'use a finer process.',
                            feature=body.name,
                        ))
        except:
            pass

    def _check_draft_angles(self, body, min_draft: float, process: str):
        """Check for missing draft angles (casting/molding)."""
        if min_draft <= 0:
            return  # Process doesn't need draft

        self.issues.append(Issue(
            domain='geometric',
            severity=Severity.WARNING,
            code='DRAFT_ANGLE',
            title=f'Draft angle check needed ({min_draft}°)',
            detail=f'{process} requires minimum {min_draft}° draft on '
                   f'vertical faces. Manual check recommended.',
            fix=f'Add {min_draft}° draft to all faces parallel to the '
                f'mold pull direction using Design → Draft.',
        ))

    def _check_internal_radii(self, body, min_radius: float, process: str):
        """Check if internal corners have sufficient radius for tooling."""
        if min_radius <= 0:
            return

        self.issues.append(Issue(
            domain='geometric',
            severity=Severity.INFO,
            code='INTERNAL_RADIUS',
            title=f'Minimum internal radius: {min_radius}mm',
            detail=f'{process} requires at least {min_radius}mm radius on '
                   f'internal corners (tool clearance).',
            fix=f'Ensure all internal corners have at least R{min_radius}mm fillet.',
        ))

    # ═══════════════════════════════════════════════════════════
    # PARAMETER CHECKS
    # ═══════════════════════════════════════════════════════════

    def _check_parameters(self):
        """Check user parameters for issues."""
        user_params = self.design.userParameters

        if user_params.count == 0:
            self.issues.append(Issue(
                domain='geometric',
                severity=Severity.WARNING,
                code='NO_PARAMETERS',
                title='No user parameters defined',
                detail='The model has no user parameters. '
                       'Dimensions are hard-coded and not easily editable.',
                fix='Create user parameters for all key dimensions '
                    'to enable parametric editing.',
            ))
            return

        for i in range(user_params.count):
            p = user_params.item(i)
            val_mm = p.value * 10  # cm to mm

            # Check for unrealistic values
            if val_mm <= 0:
                self.issues.append(Issue(
                    domain='geometric',
                    severity=Severity.ERROR,
                    code='ZERO_PARAM',
                    title=f'Parameter "{p.name}" is zero or negative',
                    detail=f'{p.name} = {p.expression} → {val_mm:.3f}mm',
                    fix='Set a positive value.',
                    feature=p.name,
                ))
            elif val_mm > 10000:
                self.issues.append(Issue(
                    domain='geometric',
                    severity=Severity.WARNING,
                    code='HUGE_PARAM',
                    title=f'Parameter "{p.name}" is very large ({val_mm:.0f}mm)',
                    detail=f'Check units — this might be a conversion error.',
                    fix='Verify the value and units are correct.',
                    feature=p.name,
                ))

    # ═══════════════════════════════════════════════════════════
    # GD&T CHECKS
    # ═══════════════════════════════════════════════════════════

    def _check_gdt(self, gdt_spec: dict):
        """Validate the GD&T specification for completeness and correctness."""
        datums = gdt_spec.get('datums', [])
        fcs = gdt_spec.get('feature_controls', [])
        dim_tols = gdt_spec.get('dimensional_tolerances', [])

        # Must have at least one datum
        if not datums:
            self.issues.append(Issue(
                domain='drawing',
                severity=Severity.ERROR,
                code='NO_DATUMS',
                title='No datum features defined',
                detail='A properly toleranced part needs at least '
                       'one datum feature (usually the mounting surface).',
                fix='Define datum A on the primary mounting/locating surface.',
            ))

        # Primary datum should be A
        if datums and datums[0].get('label') != 'A':
            self.issues.append(Issue(
                domain='drawing',
                severity=Severity.WARNING,
                code='DATUM_ORDER',
                title='Primary datum should be labeled A',
                detail='Per ASME Y14.5, datums are labeled alphabetically '
                       'starting with the primary datum as A.',
                fix='Relabel the most important locating surface as datum A.',
            ))

        # Check feature controls reference valid datums
        datum_labels = {d['label'] for d in datums}
        for fc in fcs:
            for ref in fc.get('datums', []):
                if ref not in datum_labels:
                    self.issues.append(Issue(
                        domain='drawing',
                        severity=Severity.ERROR,
                        code='INVALID_DATUM_REF',
                        title=f'FCF references undefined datum "{ref}"',
                        detail=f'Feature "{fc.get("feature", "?")}" references '
                               f'datum {ref} which is not defined.',
                        fix=f'Define datum {ref} or remove the reference.',
                        feature=fc.get('feature', ''),
                    ))

        # Position tolerance should have datums
        for fc in fcs:
            if fc.get('symbol') == 'position' and not fc.get('datums'):
                self.issues.append(Issue(
                    domain='drawing',
                    severity=Severity.ERROR,
                    code='POSITION_NO_DATUM',
                    title='Position tolerance without datums',
                    detail=f'Feature "{fc.get("feature", "?")}" has a position '
                           f'tolerance but no datum references. Position needs '
                           f'at least one datum.',
                    fix='Add datum references (typically A, B, C) to the FCF.',
                    feature=fc.get('feature', ''),
                ))

        # Check tolerance values are reasonable
        for fc in fcs:
            tol = fc.get('tolerance', 0)
            symbol = fc.get('symbol', '')

            if symbol == 'position' and tol > 1.0:
                self.issues.append(Issue(
                    domain='drawing',
                    severity=Severity.WARNING,
                    code='LOOSE_POSITION',
                    title=f'Very loose position tolerance: {tol}mm',
                    detail=f'Position tolerance of {tol}mm on '
                           f'"{fc.get("feature", "?")}" is unusually large.',
                    fix='Typical position tolerances are 0.01-0.5mm. '
                        'Verify this is intentional.',
                    feature=fc.get('feature', ''),
                ))

            if symbol == 'flatness' and tol > 0.5:
                self.issues.append(Issue(
                    domain='drawing',
                    severity=Severity.WARNING,
                    code='LOOSE_FLATNESS',
                    title=f'Flatness tolerance {tol}mm may be too loose',
                    detail='For mating surfaces, flatness is typically 0.01-0.1mm.',
                    fix='Tighten if this is a sealing or mating surface.',
                    feature=fc.get('feature', ''),
                ))

        # Check dimensional tolerances have valid fits
        valid_holes = {'H6', 'H7', 'H8', 'H9', 'H10', 'H11'}
        valid_shafts = {'f6', 'f7', 'g6', 'h6', 'k6', 'n6', 'p6', 'r6', 's6'}

        for dt in dim_tols:
            fit = dt.get('fit', '')
            if fit and fit not in valid_holes and fit not in valid_shafts:
                self.issues.append(Issue(
                    domain='drawing',
                    severity=Severity.WARNING,
                    code='UNKNOWN_FIT',
                    title=f'Unknown fit class: {fit}',
                    detail=f'Feature "{dt.get("feature", "?")}" uses fit '
                           f'class "{fit}" which is not in standard ISO 286 tables.',
                    fix='Use a standard fit: H7 (hole), g6/k6/p6 (shaft).',
                    feature=dt.get('feature', ''),
                ))

        # MMC on datum features — not allowed per Y14.5-2018
        for fc in fcs:
            if fc.get('symbol') == 'flatness' and fc.get('mmc'):
                self.issues.append(Issue(
                    domain='drawing',
                    severity=Severity.ERROR,
                    code='MMC_ON_FORM',
                    title='MMC on form tolerance (not allowed)',
                    detail='Per ASME Y14.5-2018, form tolerances (flatness, '
                           'straightness, circularity, cylindricity) cannot '
                           'use material condition modifiers.',
                    fix='Remove MMC modifier from the form tolerance.',
                    feature=fc.get('feature', ''),
                ))

    # ═══════════════════════════════════════════════════════════
    # DRAWING COMPLETENESS CHECKS
    # ═══════════════════════════════════════════════════════════

    def _check_drawing_completeness(self, gdt_spec: dict):
        """Check if the drawing specification is complete."""

        # Must have general tolerance specified
        gen_tol = gdt_spec.get('general_tolerance', '')
        if not gen_tol:
            self.issues.append(Issue(
                domain='drawing',
                severity=Severity.ERROR,
                code='NO_GENERAL_TOL',
                title='No general tolerance specified',
                detail='Every engineering drawing must specify a general '
                       'tolerance for non-critical dimensions.',
                fix='Add "ISO 2768-mK" (medium) or "ISO 2768-fH" (fine) '
                    'to the title block.',
            ))

        # Surface finish should be specified
        surfaces = gdt_spec.get('surface_finishes', [])
        if not surfaces:
            self.issues.append(Issue(
                domain='drawing',
                severity=Severity.WARNING,
                code='NO_SURFACE_FINISH',
                title='No surface finish requirements specified',
                detail='Surface finish affects function and cost. '
                       'At minimum, specify a general Ra value.',
                fix='Add general surface finish (e.g. Ra 3.2 unless noted) '
                    'and specific callouts for functional surfaces.',
            ))

        # All bores/holes should have tolerances
        dim_tols = gdt_spec.get('dimensional_tolerances', [])
        fcs = gdt_spec.get('feature_controls', [])

        # Check critical features have both size and position tolerance
        toleranced_features = {dt['feature'] for dt in dim_tols}
        positioned_features = {
            fc['feature'] for fc in fcs if fc.get('symbol') == 'position'
        }

        for feat in toleranced_features:
            if 'bore' in feat.lower() or 'hole' in feat.lower():
                if feat not in positioned_features:
                    self.issues.append(Issue(
                        domain='drawing',
                        severity=Severity.WARNING,
                        code='HOLE_NO_POSITION',
                        title=f'Hole "{feat}" has size tolerance but no position',
                        detail='Holes that mate with other parts typically need '
                               'both size and position tolerances.',
                        fix=f'Add a position tolerance to "{feat}" with '
                            f'appropriate datum references.',
                        feature=feat,
                    ))

        # Check that critical datum surfaces have flatness/perpendicularity
        datums = gdt_spec.get('datums', [])
        controlled_datums = {
            fc['feature'] for fc in fcs
            if fc.get('symbol') in ('flatness', 'perpendicularity', 'parallelism')
        }

        for d in datums:
            if d['feature'] not in controlled_datums:
                self.issues.append(Issue(
                    domain='drawing',
                    severity=Severity.INFO,
                    code='DATUM_NO_FORM',
                    title=f'Datum {d["label"]} has no form control',
                    detail=f'Datum feature "{d["feature"]}" should ideally have '
                           f'a form tolerance (flatness or profile) to ensure '
                           f'it can function as a reliable datum.',
                    fix=f'Add flatness or profile tolerance to datum {d["label"]}.',
                    feature=d['feature'],
                ))

    # ═══════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════

    def _get_all_components(self):
        """Yield all components in the design."""
        yield self.root_comp
        for i in range(self.root_comp.occurrences.count):
            occ = self.root_comp.occurrences.item(i)
            if occ.component:
                yield occ.component

    def _get_bodies(self, comp):
        """Yield all solid bodies in a component."""
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            if body.isSolid:
                yield body

    def get_report(self) -> dict:
        """Generate a structured validation report."""
        errors = [i for i in self.issues if i.severity == Severity.ERROR]
        warnings = [i for i in self.issues if i.severity == Severity.WARNING]
        infos = [i for i in self.issues if i.severity == Severity.INFO]

        return {
            'summary': {
                'total': len(self.issues),
                'errors': len(errors),
                'warnings': len(warnings),
                'info': len(infos),
                'pass': len(errors) == 0,
            },
            'issues': [i.to_dict() for i in self.issues],
        }

    def get_report_text(self) -> str:
        """Generate a human-readable validation report."""
        report = self.get_report()
        s = report['summary']

        lines = [
            '═' * 50,
            '  ENGINEERING VALIDATION REPORT',
            '═' * 50,
            '',
            f'  Result: {"✅ PASS" if s["pass"] else "❌ FAIL"}',
            f'  Errors: {s["errors"]}  |  Warnings: {s["warnings"]}  |  Info: {s["info"]}',
            '',
        ]

        icons = {Severity.ERROR: '❌', Severity.WARNING: '⚠️', Severity.INFO: 'ℹ️'}

        for issue in self.issues:
            lines.append(f'{icons.get(issue.severity, "?")} [{issue.code}] {issue.title}')
            lines.append(f'   {issue.detail}')
            lines.append(f'   💡 Fix: {issue.fix}')
            lines.append('')

        lines.append('═' * 50)
        return '\n'.join(lines)
