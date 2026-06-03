"""
validation_engine.py — Quality Validation Engine for TextToCAD Pro.

Performs comprehensive checks on:
1. MODEL GEOMETRY   — Is the 3D model physically valid?
2. STRUCTURAL        — Will the part survive its intended use?
3. DFM              — Can this actually be manufactured?
4. DRAWING          — Is the 2D drawing complete and correct per standards?
5. GD&T             — Are the tolerances consistent and achievable?

Each check returns a structured report with severity levels:
  ERROR   = Must fix, part will fail or can't be made
  WARNING = Should fix, potential issue
  INFO    = Suggestion for improvement
"""

import math
import json

try:
    import adsk.core
    import adsk.fusion
    HAS_FUSION = True
except ImportError:
    HAS_FUSION = False


# ═══════════════════════════════════════════════════════════════
# Severity levels
# ═══════════════════════════════════════════════════════════════
ERROR = 'ERROR'
WARNING = 'WARNING'
INFO = 'INFO'


class ValidationIssue:
    """Single validation finding."""
    def __init__(self, severity: str, category: str, rule_id: str,
                 message: str, feature: str = '', suggestion: str = '',
                 location: str = ''):
        self.severity = severity
        self.category = category
        self.rule_id = rule_id
        self.message = message
        self.feature = feature
        self.suggestion = suggestion
        self.location = location  # face/edge/body name or coordinates

    def to_dict(self) -> dict:
        return {
            'severity': self.severity,
            'category': self.category,
            'rule_id': self.rule_id,
            'message': self.message,
            'feature': self.feature,
            'suggestion': self.suggestion,
            'location': self.location,
        }

    def __repr__(self):
        return f'[{self.severity}] {self.category}/{self.rule_id}: {self.message}'


class ValidationReport:
    """Collection of issues with summary statistics."""
    def __init__(self):
        self.issues = []
        self.metadata = {}

    def add(self, issue: ValidationIssue):
        self.issues.append(issue)

    @property
    def errors(self):
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self):
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def infos(self):
        return [i for i in self.issues if i.severity == INFO]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def score(self) -> int:
        """Quality score 0-100. Starts at 100, deduct for issues."""
        s = 100
        s -= len(self.errors) * 15
        s -= len(self.warnings) * 5
        s -= len(self.infos) * 1
        return max(0, min(100, s))

    def summary(self) -> str:
        lines = []
        lines.append(f'Validation Score: {self.score}/100')
        lines.append(f'  Errors:   {len(self.errors)}')
        lines.append(f'  Warnings: {len(self.warnings)}')
        lines.append(f'  Info:     {len(self.infos)}')
        if not self.is_valid:
            lines.append('\nCritical issues:')
            for e in self.errors:
                lines.append(f'  [{e.rule_id}] {e.message}')
                if e.suggestion:
                    lines.append(f'    → {e.suggestion}')
        return '\n'.join(lines)

    def to_dict(self) -> dict:
        return {
            'score': self.score,
            'is_valid': self.is_valid,
            'error_count': len(self.errors),
            'warning_count': len(self.warnings),
            'info_count': len(self.infos),
            'issues': [i.to_dict() for i in self.issues],
            'metadata': self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ═══════════════════════════════════════════════════════════════
# 1. MODEL GEOMETRY VALIDATOR
# ═══════════════════════════════════════════════════════════════
class ModelGeometryValidator:
    """
    Checks the 3D model for geometric validity.
    Works with Fusion 360 API objects when available,
    or with extracted geometry data from the AI.
    """

    def validate(self, component, report: ValidationReport):
        """Run all geometry checks on a Fusion component."""
        if not HAS_FUSION or component is None:
            return

        self._check_empty_bodies(component, report)
        self._check_zero_volume(component, report)
        self._check_self_intersection(component, report)
        self._check_open_bodies(component, report)
        self._check_tiny_features(component, report)
        self._check_sharp_edges(component, report)
        self._check_sketch_health(component, report)
        self._check_feature_errors(component, report)

    def _check_empty_bodies(self, comp, report):
        bodies = comp.bRepBodies
        if bodies.count == 0:
            report.add(ValidationIssue(
                ERROR, 'GEOMETRY', 'GEO-001',
                'Component has no solid bodies.',
                suggestion='Check that extrusions and features completed successfully.'
            ))

    def _check_zero_volume(self, comp, report):
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                vol = body.volume  # cm³
                if vol < 1e-6:
                    report.add(ValidationIssue(
                        ERROR, 'GEOMETRY', 'GEO-002',
                        f'Body "{body.name}" has near-zero volume ({vol:.2e} cm³).',
                        feature=body.name,
                        suggestion='Body may be a surface or degenerate solid.'
                    ))
                elif vol < 0.001:  # Less than 1mm³
                    report.add(ValidationIssue(
                        WARNING, 'GEOMETRY', 'GEO-003',
                        f'Body "{body.name}" is extremely small ({vol:.4f} cm³).',
                        feature=body.name,
                        suggestion='Verify this is intentional.'
                    ))
            except:
                pass

    def _check_self_intersection(self, comp, report):
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                if not body.isSolid:
                    report.add(ValidationIssue(
                        ERROR, 'GEOMETRY', 'GEO-004',
                        f'Body "{body.name}" is not a valid solid (may have self-intersections).',
                        feature=body.name,
                        suggestion='Check for overlapping features or bad boolean operations.'
                    ))
            except:
                pass

    def _check_open_bodies(self, comp, report):
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                if not body.isSolid:
                    # Check if it's a surface body
                    shells = body.shells
                    for j in range(shells.count):
                        shell = shells.item(j)
                        if not shell.isClosed:
                            report.add(ValidationIssue(
                                ERROR, 'GEOMETRY', 'GEO-005',
                                f'Body "{body.name}" has open shell (not watertight).',
                                feature=body.name,
                                suggestion='Close all surface gaps before manufacturing.'
                            ))
            except:
                pass

    def _check_tiny_features(self, comp, report):
        """Detect faces that are unusually small (< 0.1mm²)."""
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                faces = body.faces
                for j in range(faces.count):
                    face = faces.item(j)
                    area = face.area  # cm²
                    if area < 1e-4:  # < 0.01mm²
                        report.add(ValidationIssue(
                            WARNING, 'GEOMETRY', 'GEO-006',
                            f'Tiny face detected on "{body.name}" (area: {area*100:.4f} mm²).',
                            feature=body.name,
                            suggestion='Tiny faces cause meshing and manufacturing issues. '
                                       'Consider merging or removing.'
                        ))
            except:
                pass

    def _check_sharp_edges(self, comp, report):
        """Check for very sharp internal edges that need fillets."""
        sharp_count = 0
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                edges = body.edges
                for j in range(edges.count):
                    edge = edges.item(j)
                    if edge.isDegenerate:
                        continue
                    # Check for concave edges (internal corners)
                    faces = edge.faces
                    if faces.count == 2:
                        try:
                            angle = faces.item(0).geometry.normal.angleTo(
                                faces.item(1).geometry.normal
                            )
                            if angle < math.radians(30):  # Very sharp internal corner
                                sharp_count += 1
                        except:
                            pass
            except:
                pass

        if sharp_count > 5:
            report.add(ValidationIssue(
                WARNING, 'GEOMETRY', 'GEO-007',
                f'{sharp_count} sharp internal edges detected.',
                suggestion='Add fillets to reduce stress concentration. '
                           'Minimum 0.5mm for machined, 1mm for cast parts.'
            ))

    def _check_sketch_health(self, comp, report):
        """Check all sketches for under/over-constrained states."""
        sketches = comp.sketches
        for i in range(sketches.count):
            sketch = sketches.item(i)
            # Count constraint status
            curves = sketch.sketchCurves
            underconstrained = 0
            for j in range(curves.count):
                try:
                    curve = curves.item(j)
                    if not curve.isFullyConstrained:
                        underconstrained += 1
                except:
                    pass

            if underconstrained > 0:
                report.add(ValidationIssue(
                    INFO, 'GEOMETRY', 'GEO-008',
                    f'Sketch "{sketch.name}" has {underconstrained} under-constrained curves.',
                    feature=sketch.name,
                    suggestion='Fully constrain sketches for robust parametric behavior.'
                ))

    def _check_feature_errors(self, comp, report):
        """Check timeline for failed or warning features."""
        try:
            design = adsk.fusion.Design.cast(comp.parentDesign)
            timeline = design.timeline
            for i in range(timeline.count):
                item = timeline.item(i)
                if item.healthState == adsk.fusion.FeatureHealthStates.ErrorFeatureHealthState:
                    report.add(ValidationIssue(
                        ERROR, 'GEOMETRY', 'GEO-009',
                        f'Feature at timeline position {i} has an error: {item.errorOrWarningMessage}',
                        suggestion='Fix or suppress the failed feature.'
                    ))
                elif item.healthState == adsk.fusion.FeatureHealthStates.WarningFeatureHealthState:
                    report.add(ValidationIssue(
                        WARNING, 'GEOMETRY', 'GEO-010',
                        f'Feature at timeline position {i} has a warning: {item.errorOrWarningMessage}',
                    ))
        except:
            pass


# ═══════════════════════════════════════════════════════════════
# 2. STRUCTURAL INTEGRITY VALIDATOR
# ═══════════════════════════════════════════════════════════════
class StructuralValidator:
    """
    Checks for structural weaknesses based on geometry analysis.
    Not a replacement for FEA, but catches obvious issues.
    """

    # Minimum wall thickness by process (mm)
    MIN_WALL = {
        'CNC Machining': 1.0,
        'Turning': 0.8,
        'Casting': 3.0,
        'Injection Molding': 0.8,
        '3D Print (FDM)': 1.2,
        '3D Print (SLA)': 0.5,
        '3D Print (Metal)': 0.4,
        'Sheet Metal': 0.5,
    }

    # Max L/D ratio for unsupported features
    MAX_ASPECT_RATIOS = {
        'pin': 8,           # Pin length / diameter
        'rib': 10,          # Rib height / thickness
        'boss': 3,          # Boss height / diameter
        'wall': 20,         # Wall height / thickness
        'hole_depth': 10,   # Hole depth / diameter (CNC)
        'cantilever': 5,    # Cantilever length / thickness
    }

    def validate(self, component, process: str, material: str,
                 report: ValidationReport):
        """Run structural checks."""
        if not HAS_FUSION or component is None:
            return

        self._check_wall_thickness(component, process, report)
        self._check_aspect_ratios(component, report)
        self._check_stress_concentrators(component, report)
        self._check_unsupported_overhangs(component, process, report)
        self._check_thin_sections(component, material, report)

    def _check_wall_thickness(self, comp, process, report):
        """Estimate wall thickness and check against minimums."""
        min_wall = self.MIN_WALL.get(process, 1.0)

        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                # Use bounding box as rough indicator
                bb = body.boundingBox
                dims = [
                    bb.maxPoint.x - bb.minPoint.x,
                    bb.maxPoint.y - bb.minPoint.y,
                    bb.maxPoint.z - bb.minPoint.z,
                ]
                min_dim = min(dims) * 10  # cm to mm
                if min_dim < min_wall:
                    report.add(ValidationIssue(
                        WARNING, 'STRUCTURAL', 'STR-001',
                        f'Body "{body.name}" minimum dimension ({min_dim:.2f}mm) '
                        f'is below minimum wall thickness for {process} ({min_wall}mm).',
                        feature=body.name,
                        suggestion=f'Increase wall thickness to at least {min_wall}mm.'
                    ))
            except:
                pass

    def _check_aspect_ratios(self, comp, report):
        """Check for features with extreme aspect ratios."""
        for i in range(comp.bRepBodies.count):
            body = comp.bRepBodies.item(i)
            try:
                bb = body.boundingBox
                dims = sorted([
                    (bb.maxPoint.x - bb.minPoint.x) * 10,
                    (bb.maxPoint.y - bb.minPoint.y) * 10,
                    (bb.maxPoint.z - bb.minPoint.z) * 10,
                ])  # mm, sorted ascending

                if dims[0] > 0.1:
                    aspect = dims[2] / dims[0]
                    if aspect > self.MAX_ASPECT_RATIOS['wall']:
                        report.add(ValidationIssue(
                            WARNING, 'STRUCTURAL', 'STR-002',
                            f'Body "{body.name}" has extreme aspect ratio ({aspect:.1f}:1). '
                            f'Thinnest: {dims[0]:.2f}mm, longest: {dims[2]:.2f}mm.',
                            feature=body.name,
                            suggestion='High aspect ratio features are prone to deflection '
                                       'and vibration. Add ribs or increase thickness.'
                        ))
            except:
                pass

    def _check_stress_concentrators(self, comp, report):
        """Flag features that concentrate stress."""
        try:
            features = comp.features
            # Check for sharp fillets (too small)
            fillets = features.filletFeatures
            for i in range(fillets.count):
                fillet = fillets.item(i)
                # Get radius
                try:
                    for j in range(fillet.edgeSets.count):
                        edge_set = fillet.edgeSets.item(j)
                        radius_cm = edge_set.radius.value if hasattr(edge_set.radius, 'value') else edge_set.radius
                        radius_mm = radius_cm * 10
                        if radius_mm < 0.3:
                            report.add(ValidationIssue(
                                WARNING, 'STRUCTURAL', 'STR-003',
                                f'Fillet radius {radius_mm:.2f}mm is very small. '
                                'May not effectively reduce stress concentration.',
                                suggestion='Consider increasing fillet radius to at least 0.5mm.'
                            ))
                except:
                    pass

            # Check for notches / keyways without fillets
            # (detect cut features that don't have adjacent fillets)
            extrudes = features.extrudeFeatures
            cut_count = 0
            for i in range(extrudes.count):
                try:
                    ext = extrudes.item(i)
                    if ext.operation == adsk.fusion.FeatureOperations.CutFeatureOperation:
                        cut_count += 1
                except:
                    pass

            fillet_count = fillets.count if fillets else 0
            if cut_count > 2 and fillet_count == 0:
                report.add(ValidationIssue(
                    WARNING, 'STRUCTURAL', 'STR-004',
                    f'{cut_count} cut features found but no fillets applied. '
                    'Sharp internal corners are stress concentrators.',
                    suggestion='Add fillets to internal corners of cut features.'
                ))
        except:
            pass

    def _check_unsupported_overhangs(self, comp, process, report):
        """Check for overhangs that need support (3D printing)."""
        if '3D Print' not in process:
            return

        report.add(ValidationIssue(
            INFO, 'STRUCTURAL', 'STR-005',
            'Part is intended for 3D printing. Check for overhangs >45° '
            'that may need support material.',
            suggestion='Orient part to minimize supports, or add chamfers '
                       'to overhanging surfaces.'
        ))

    def _check_thin_sections(self, comp, material, report):
        """Check for thin sections that may deform under load."""
        # Material-specific minimum recommendations
        thin_limits = {
            'Steel': 1.5, 'Aluminum': 2.0, 'Stainless Steel': 1.5,
            'Cast Iron': 3.0, 'Brass': 1.5, 'Titanium': 1.0,
            'Plastic (ABS)': 1.0, 'Nylon': 0.8,
        }
        limit = thin_limits.get(material, 1.5)

        # This is a simplified check — real analysis needs ray casting
        report.add(ValidationIssue(
            INFO, 'STRUCTURAL', 'STR-006',
            f'Recommended minimum section thickness for {material}: {limit}mm. '
            'Verify all sections meet this requirement.',
            suggestion='Use Fusion 360 section analysis to inspect wall thickness.'
        ))


# ═══════════════════════════════════════════════════════════════
# 3. DFM (Design for Manufacturability) VALIDATOR
# ═══════════════════════════════════════════════════════════════
class DFMValidator:
    """
    Checks if the part can be manufactured by the specified process.
    """

    def validate(self, component, process: str, material: str,
                 params: dict, report: ValidationReport):
        """Run DFM checks based on manufacturing process."""
        if not HAS_FUSION or component is None:
            return

        # Universal checks
        self._check_undercuts(component, process, report)
        self._check_hole_standards(params, report)
        self._check_thread_standards(component, report)

        # Process-specific checks
        if 'CNC' in process or 'Machining' in process:
            self._check_cnc(component, params, report)
        elif 'Turning' in process:
            self._check_turning(component, report)
        elif 'Casting' in process:
            self._check_casting(component, report)
        elif 'Injection' in process:
            self._check_injection_molding(component, report)
        elif '3D Print' in process:
            self._check_3d_printing(component, process, report)
        elif 'Sheet Metal' in process:
            self._check_sheet_metal(component, report)

    def _check_undercuts(self, comp, process, report):
        """Check for undercuts that are hard to machine."""
        if 'CNC' in process or 'Turning' in process:
            report.add(ValidationIssue(
                INFO, 'DFM', 'DFM-001',
                'Verify no internal undercuts require special tooling. '
                'AI cannot fully analyze tool accessibility.',
                suggestion='Use section analysis to verify all internal features '
                           'are accessible from at least one direction.'
            ))

    def _check_hole_standards(self, params, report):
        """Check that hole diameters are standard drill sizes."""
        STANDARD_DRILLS_MM = [
            1.0, 1.5, 2.0, 2.5, 3.0, 3.3, 3.5, 4.0, 4.2, 4.5, 5.0, 5.5,
            6.0, 6.5, 7.0, 8.0, 8.5, 9.0, 10.0, 10.5, 11.0, 12.0, 13.0,
            14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 22.0, 24.0, 25.0,
            26.0, 28.0, 30.0, 32.0, 35.0, 38.0, 40.0, 42.0, 45.0, 48.0, 50.0
        ]

        for name, info in params.items():
            if any(kw in name.lower() for kw in ['hole', 'bore', 'drill']):
                val_mm = info.get('value', 0) * 10  # cm to mm
                if val_mm > 0:
                    # Find nearest standard
                    nearest = min(STANDARD_DRILLS_MM, key=lambda d: abs(d - val_mm))
                    if abs(nearest - val_mm) > 0.05:
                        report.add(ValidationIssue(
                            WARNING, 'DFM', 'DFM-002',
                            f'Parameter "{name}" = {val_mm:.2f}mm is not a standard drill size.',
                            feature=name,
                            suggestion=f'Consider using standard drill size: {nearest}mm '
                                       f'(difference: {abs(nearest - val_mm):.3f}mm).'
                        ))

    def _check_thread_standards(self, comp, report):
        """Check that threads use standard sizes."""
        try:
            threads = comp.features.threadFeatures
            if threads.count > 0:
                report.add(ValidationIssue(
                    INFO, 'DFM', 'DFM-003',
                    f'{threads.count} thread feature(s) found. '
                    'Verify thread depth is at least 1.5× diameter for steel, '
                    '2× diameter for aluminum.',
                ))
        except:
            pass

    def _check_cnc(self, comp, params, report):
        """CNC-specific checks."""
        # Internal corner radius
        try:
            fillets = comp.features.filletFeatures
            for i in range(fillets.count):
                fillet = fillets.item(i)
                for j in range(fillet.edgeSets.count):
                    edge_set = fillet.edgeSets.item(j)
                    try:
                        r_mm = edge_set.radius.value * 10 if hasattr(edge_set.radius, 'value') else edge_set.radius * 10
                        if r_mm < 0.5:
                            report.add(ValidationIssue(
                                WARNING, 'DFM', 'DFM-010',
                                f'Internal fillet radius {r_mm:.2f}mm requires very small end mill.',
                                suggestion='Minimum 1mm internal radius for standard tooling, '
                                           '0.5mm for micro-machining.'
                            ))
                    except:
                        pass
        except:
            pass

        # Deep pockets
        for name, info in params.items():
            if 'depth' in name.lower() or 'height' in name.lower():
                val_mm = info.get('value', 0) * 10
                if val_mm > 50:
                    report.add(ValidationIssue(
                        INFO, 'DFM', 'DFM-011',
                        f'Deep feature "{name}" = {val_mm:.1f}mm may require '
                        'long-reach tooling or multiple setups.',
                        feature=name,
                    ))

    def _check_turning(self, comp, report):
        """Lathe-specific checks."""
        report.add(ValidationIssue(
            INFO, 'DFM', 'DFM-020',
            'For turned parts: verify all features are axially symmetric '
            'and accessible from one side. Non-symmetric features require milling ops.',
        ))

    def _check_casting(self, comp, report):
        """Casting-specific checks."""
        report.add(ValidationIssue(
            INFO, 'DFM', 'DFM-030',
            'For casting: verify uniform wall thickness (avoid thick/thin transitions), '
            'draft angles of 1-3° on all vertical walls, and no sharp internal corners.',
            suggestion='Add 3° draft to vertical walls and 3mm minimum fillets.'
        ))

        # Check for draft angles (simplified)
        try:
            draft_features = comp.features.draftFeatures
            if draft_features.count == 0:
                report.add(ValidationIssue(
                    WARNING, 'DFM', 'DFM-031',
                    'No draft features found on a casting part. '
                    'Most vertical walls need 1-3° draft for mold release.',
                    suggestion='Add draft features to vertical faces.'
                ))
        except:
            pass

    def _check_injection_molding(self, comp, report):
        """Injection molding checks."""
        checks = [
            ('DFM-040', 'Uniform wall thickness recommended (±10% variation max).'),
            ('DFM-041', 'Ribs should be 50-70% of adjacent wall thickness.'),
            ('DFM-042', 'Boss diameter should be 2x hole diameter.'),
            ('DFM-043', 'Minimum draft angle 0.5° per side, 1° recommended.'),
            ('DFM-044', 'Avoid sharp corners — use minimum 0.5mm radius everywhere.'),
        ]
        for rule_id, msg in checks:
            report.add(ValidationIssue(INFO, 'DFM', rule_id, msg))

        # Check for shell feature (indicates hollow part)
        try:
            shells = comp.features.shellFeatures
            if shells.count == 0:
                report.add(ValidationIssue(
                    WARNING, 'DFM', 'DFM-045',
                    'No shell feature found. Injection molded parts should be '
                    'hollow (shelled) to avoid sink marks and warping.',
                    suggestion='Apply a shell feature with appropriate wall thickness.'
                ))
        except:
            pass

    def _check_3d_printing(self, comp, process, report):
        """3D printing checks."""
        if 'FDM' in process:
            min_wall, min_detail = 1.2, 0.8
        elif 'SLA' in process:
            min_wall, min_detail = 0.5, 0.3
        elif 'Metal' in process:
            min_wall, min_detail = 0.4, 0.5
        else:
            min_wall, min_detail = 1.0, 0.5

        report.add(ValidationIssue(
            INFO, 'DFM', 'DFM-050',
            f'For {process}: minimum wall {min_wall}mm, '
            f'minimum detail {min_detail}mm. '
            'Overhangs >45° need support.',
        ))

    def _check_sheet_metal(self, comp, report):
        """Sheet metal checks."""
        report.add(ValidationIssue(
            INFO, 'DFM', 'DFM-060',
            'Sheet metal: minimum bend radius = material thickness. '
            'Hole diameter ≥ material thickness. '
            'Edge-to-bend distance ≥ 2× material thickness.',
        ))


# ═══════════════════════════════════════════════════════════════
# 4. DRAWING COMPLETENESS VALIDATOR
# ═══════════════════════════════════════════════════════════════
class DrawingValidator:
    """
    Checks a 2D drawing for completeness and standards compliance.
    Can work with drawing API objects or with a checklist approach.
    """

    def validate_checklist(self, gdt_spec: dict, params: dict,
                           has_drawing: bool, report: ValidationReport):
        """
        Validate drawing completeness based on available data.
        Works even without Fusion drawing API access.
        """
        self._check_views(report, has_drawing)
        self._check_dimensions(params, report)
        self._check_gdt_completeness(gdt_spec, report)
        self._check_title_block(report, has_drawing)
        self._check_notes(report)

    def _check_views(self, report, has_drawing):
        if not has_drawing:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-001',
                'No mechanical drawing created. A complete engineering package '
                'requires 2D drawings with dimensions and GD&T.',
                suggestion='Enable drawing generation in output settings.'
            ))
            return

        required_views = ['Front', 'Top or Right side', 'Isometric']
        report.add(ValidationIssue(
            INFO, 'DRAWING', 'DWG-002',
            f'Verify drawing has all required views: {", ".join(required_views)}. '
            'Add section views for internal features (bores, pockets).'
        ))

    def _check_dimensions(self, params, report):
        if not params:
            report.add(ValidationIssue(
                ERROR, 'DRAWING', 'DWG-010',
                'No user parameters found. Drawing will have no dimensions.',
                suggestion='Ensure the 3D model was built with user parameters.'
            ))
            return

        # Check that critical dimensions exist
        critical_keywords = ['dia', 'bore', 'length', 'width', 'height', 'thickness']
        found_critical = False
        for name in params:
            if any(kw in name.lower() for kw in critical_keywords):
                found_critical = True
                break

        if not found_critical:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-011',
                'No clearly named critical dimensions found (diameter, bore, length, etc.).',
                suggestion='Use descriptive parameter names for drawing auto-dimensioning.'
            ))

        # Check that all dimensions have units
        for name, info in params.items():
            if not info.get('unit'):
                report.add(ValidationIssue(
                    WARNING, 'DRAWING', 'DWG-012',
                    f'Parameter "{name}" has no unit specified.',
                    feature=name,
                ))

    def _check_gdt_completeness(self, gdt_spec, report):
        if not gdt_spec:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-020',
                'No GD&T specification generated. Drawing will lack '
                'geometric tolerances and datum references.',
            ))
            return

        datums = gdt_spec.get('datums', [])
        fcs = gdt_spec.get('feature_controls', [])
        surfaces = gdt_spec.get('surface_finishes', [])
        dim_tols = gdt_spec.get('dimensional_tolerances', [])

        if len(datums) == 0:
            report.add(ValidationIssue(
                ERROR, 'DRAWING', 'DWG-021',
                'No datums defined. Every functional drawing needs at least '
                'one datum (primary mounting/locating surface).',
                suggestion='Define datums A, B, C on the primary mounting surfaces.'
            ))

        if len(datums) > 0 and len(fcs) == 0:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-022',
                'Datums defined but no feature control frames. '
                'Add position, perpendicularity, or other controls as needed.',
            ))

        if len(surfaces) == 0:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-023',
                'No surface finish specifications. Critical surfaces '
                '(bores, mating faces, seals) need Ra callouts.',
                suggestion='Add surface finish symbols for functional surfaces.'
            ))

        if len(dim_tols) == 0:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-024',
                'No dimensional tolerances with fit classes defined. '
                'Mating features need specific tolerances (e.g. H7/g6).',
            ))

        # Check for over-tolerancing
        tight_count = sum(1 for dt in dim_tols if dt.get('upper', 1) - dt.get('lower', 0) < 0.02)
        if tight_count > 3:
            report.add(ValidationIssue(
                WARNING, 'DRAWING', 'DWG-025',
                f'{tight_count} dimensions have tolerances tighter than ±0.01mm. '
                'Over-tolerancing increases cost significantly.',
                suggestion='Only tighten tolerances where functionally necessary.'
            ))

    def _check_title_block(self, report, has_drawing):
        if has_drawing:
            report.add(ValidationIssue(
                INFO, 'DRAWING', 'DWG-030',
                'Verify title block contains: part name, part number, material, '
                'scale, drawn by, date, general tolerance, surface finish default.'
            ))

    def _check_notes(self, report):
        required_notes = [
            'General tolerances (ISO 2768-mK or equivalent)',
            'Default surface finish (Ra 3.2 unless noted)',
            'Deburr all sharp edges',
            'Material specification',
        ]
        report.add(ValidationIssue(
            INFO, 'DRAWING', 'DWG-040',
            f'Drawing should include general notes: {", ".join(required_notes)}.',
        ))


# ═══════════════════════════════════════════════════════════════
# 5. GD&T CONSISTENCY VALIDATOR
# ═══════════════════════════════════════════════════════════════
class GDTValidator:
    """
    Validates GD&T specification for internal consistency
    and engineering correctness.
    """

    def validate(self, gdt_spec: dict, process: str, report: ValidationReport):
        if not gdt_spec:
            return

        self._check_datum_hierarchy(gdt_spec, report)
        self._check_fc_datum_refs(gdt_spec, report)
        self._check_tolerance_achievability(gdt_spec, process, report)
        self._check_redundant_controls(gdt_spec, report)
        self._check_mmc_applicability(gdt_spec, report)

    def _check_datum_hierarchy(self, spec, report):
        """Datums should follow A, B, C order."""
        datums = spec.get('datums', [])
        labels = [d['label'] for d in datums]

        if labels and labels[0] != 'A':
            report.add(ValidationIssue(
                WARNING, 'GDT', 'GDT-001',
                f'Primary datum is "{labels[0]}", expected "A".',
                suggestion='Per ASME Y14.5, primary datum should be labeled A.'
            ))

        # Check for gaps
        expected = 'ABCDEFG'
        for i, label in enumerate(sorted(labels)):
            if i < len(expected) and label != expected[i]:
                report.add(ValidationIssue(
                    INFO, 'GDT', 'GDT-002',
                    f'Datum sequence has a gap (found {",".join(sorted(labels))}).',
                    suggestion='Use sequential letters: A, B, C, ...'
                ))
                break

    def _check_fc_datum_refs(self, spec, report):
        """Feature controls must reference valid datums in correct order."""
        datum_labels = {d['label'] for d in spec.get('datums', [])}

        for fc in spec.get('feature_controls', []):
            refs = fc.get('datums', [])

            # Check all referenced datums exist
            for ref in refs:
                if ref not in datum_labels:
                    report.add(ValidationIssue(
                        ERROR, 'GDT', 'GDT-010',
                        f'Feature control for "{fc["feature"]}" references '
                        f'datum "{ref}" which is not defined.',
                        feature=fc['feature'],
                        suggestion=f'Define datum {ref} or remove the reference.'
                    ))

            # Flatness, circularity, cylindricity should have NO datums
            no_datum_types = {'flatness', 'circularity', 'cylindricity', 'straightness'}
            if fc.get('symbol') in no_datum_types and refs:
                report.add(ValidationIssue(
                    ERROR, 'GDT', 'GDT-011',
                    f'{fc["symbol"]} on "{fc["feature"]}" should not reference datums. '
                    'Form tolerances are datum-independent.',
                    feature=fc['feature'],
                    suggestion=f'Remove datum references from {fc["symbol"]} callout.'
                ))

            # Position should have at least one datum
            if fc.get('symbol') == 'position' and not refs:
                report.add(ValidationIssue(
                    ERROR, 'GDT', 'GDT-012',
                    f'Position tolerance on "{fc["feature"]}" has no datum references.',
                    feature=fc['feature'],
                    suggestion='Position requires at least one datum reference.'
                ))

    def _check_tolerance_achievability(self, spec, process, report):
        """Check if tolerances are achievable by the manufacturing process."""
        process_capability = {
            'CNC Machining': 0.01,
            'Turning': 0.005,
            'Casting': 0.5,
            'Injection Molding': 0.05,
            '3D Print (FDM)': 0.3,
            '3D Print (SLA)': 0.1,
            '3D Print (Metal)': 0.05,
            'Sheet Metal': 0.1,
        }
        min_achievable = process_capability.get(process, 0.05)

        for fc in spec.get('feature_controls', []):
            tol = fc.get('tolerance', 0)
            if tol < min_achievable:
                report.add(ValidationIssue(
                    WARNING, 'GDT', 'GDT-020',
                    f'{fc["symbol"]} tolerance {tol}mm on "{fc["feature"]}" '
                    f'may be too tight for {process} '
                    f'(typical capability: ±{min_achievable}mm).',
                    feature=fc['feature'],
                    suggestion=f'Consider relaxing to {min_achievable}mm or '
                               'specify a more precise process.'
                ))

    def _check_redundant_controls(self, spec, report):
        """Check for redundant or conflicting GD&T callouts."""
        features_controlled = {}
        for fc in spec.get('feature_controls', []):
            feat = fc['feature']
            sym = fc['symbol']
            if feat not in features_controlled:
                features_controlled[feat] = []
            features_controlled[feat].append(sym)

        for feat, symbols in features_controlled.items():
            # Cylindricity makes circularity + straightness redundant
            if 'cylindricity' in symbols and 'circularity' in symbols:
                report.add(ValidationIssue(
                    WARNING, 'GDT', 'GDT-030',
                    f'"{feat}" has both cylindricity and circularity. '
                    'Cylindricity already includes circularity.',
                    feature=feat,
                    suggestion='Remove circularity — cylindricity is sufficient.'
                ))

            # Total runout makes runout redundant
            if 'total_runout' in symbols and 'runout' in symbols:
                report.add(ValidationIssue(
                    WARNING, 'GDT', 'GDT-031',
                    f'"{feat}" has both total runout and circular runout. '
                    'Total runout is the stricter control.',
                    feature=feat,
                    suggestion='Remove circular runout — total runout is sufficient.'
                ))

    def _check_mmc_applicability(self, spec, report):
        """MMC only applies to features of size."""
        for fc in spec.get('feature_controls', []):
            if fc.get('mmc'):
                non_size_types = {'flatness', 'straightness', 'circularity',
                                  'cylindricity', 'profile_surface', 'profile_line'}
                if fc.get('symbol') in non_size_types:
                    report.add(ValidationIssue(
                        ERROR, 'GDT', 'GDT-040',
                        f'MMC modifier on {fc["symbol"]} for "{fc["feature"]}" '
                        'is invalid. MMC only applies to features of size '
                        '(holes, pins, slots, tabs).',
                        feature=fc['feature'],
                        suggestion='Remove MMC modifier from this control.'
                    ))


# ═══════════════════════════════════════════════════════════════
# MASTER VALIDATOR — Runs everything
# ═══════════════════════════════════════════════════════════════
class MasterValidator:
    """
    Orchestrates all validation checks and produces a complete report.
    """

    def __init__(self):
        self.geo_validator = ModelGeometryValidator()
        self.struct_validator = StructuralValidator()
        self.dfm_validator = DFMValidator()
        self.dwg_validator = DrawingValidator()
        self.gdt_validator = GDTValidator()

    def validate_all(self, component=None, process: str = 'CNC Machining',
                     material: str = 'Steel', params: dict = None,
                     gdt_spec: dict = None, has_drawing: bool = False) -> ValidationReport:
        """
        Run the complete validation suite.

        Args:
            component: Fusion 360 component (or None for offline checks)
            process: Manufacturing process
            material: Material type
            params: User parameters dict
            gdt_spec: GD&T specification dict
            has_drawing: Whether a drawing was generated

        Returns:
            ValidationReport with all findings
        """
        report = ValidationReport()
        report.metadata = {
            'process': process,
            'material': material,
            'has_component': component is not None,
            'has_gdt': gdt_spec is not None,
            'has_drawing': has_drawing,
            'param_count': len(params) if params else 0,
        }

        # 1. Geometry checks
        self.geo_validator.validate(component, report)

        # 2. Structural checks
        self.struct_validator.validate(component, process, material, report)

        # 3. DFM checks
        self.dfm_validator.validate(component, process, material, params or {}, report)

        # 4. Drawing completeness
        self.dwg_validator.validate_checklist(gdt_spec, params or {}, has_drawing, report)

        # 5. GD&T consistency
        self.gdt_validator.validate(gdt_spec, process, report)

        return report
