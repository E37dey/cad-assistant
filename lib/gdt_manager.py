"""
gdt_manager.py — Manages GD&T (Geometric Dimensioning & Tolerancing) data.
Validates GD&T specifications, stores tolerance info with parameters,
and provides data for drawing creation.
"""

import json
import math

# ── ISO 286 Hole Basis Tolerance Tables (in mm) ──
# Format: {grade: {nom_range_max: tolerance_μm}}
# Fundamental deviations for common fits

ISO_286_HOLES = {
    'H6': {3: 6, 6: 8, 10: 9, 18: 11, 30: 13, 50: 16, 80: 19, 120: 22, 180: 25},
    'H7': {3: 10, 6: 12, 10: 15, 18: 18, 30: 21, 50: 25, 80: 30, 120: 35, 180: 40},
    'H8': {3: 14, 6: 18, 10: 22, 18: 27, 30: 33, 50: 39, 80: 46, 120: 54, 180: 63},
    'H9': {3: 25, 6: 30, 10: 36, 18: 43, 30: 52, 50: 62, 80: 74, 120: 87, 180: 100},
}

ISO_286_SHAFTS = {
    'f6': {3: (-6, -12), 6: (-10, -18), 10: (-13, -22), 18: (-16, -27),
           30: (-20, -33), 50: (-25, -41), 80: (-30, -49), 120: (-36, -58)},
    'g6': {3: (-2, -8), 6: (-4, -12), 10: (-5, -14), 18: (-6, -17),
           30: (-7, -20), 50: (-9, -25), 80: (-10, -29), 120: (-12, -34)},
    'k6': {3: (0, 6), 6: (1, 9), 10: (1, 10), 18: (1, 12),
           30: (2, 15), 50: (2, 18), 80: (2, 21), 120: (3, 25)},
    'n6': {3: (4, 10), 6: (8, 16), 10: (10, 19), 18: (12, 23),
           30: (15, 28), 50: (17, 33), 80: (20, 39), 120: (23, 45)},
    'p6': {3: (6, 12), 6: (12, 20), 10: (15, 24), 18: (18, 29),
           30: (22, 35), 50: (26, 42), 80: (32, 51), 120: (37, 59)},
    's6': {3: (10, 16), 6: (16, 24), 10: (19, 28), 18: (23, 34),
           30: (28, 41), 50: (34, 50), 80: (43, 62), 120: (52, 74)},
}

# ── GD&T Symbol Unicode ──
GDT_SYMBOLS = {
    'flatness': '⏥',
    'straightness': '⏤',
    'circularity': '○',
    'cylindricity': '⌭',
    'parallelism': '∥',
    'perpendicularity': '⊥',
    'angularity': '∠',
    'position': '⊕',
    'concentricity': '◎',
    'symmetry': '⌯',
    'runout': '↗',
    'total_runout': '↗↗',
    'profile_line': '⌒',
    'profile_surface': '⌓',
}

GDT_MODIFIERS = {
    'MMC': 'Ⓜ',
    'LMC': 'Ⓛ',
    'RFS': '',  # default in Y14.5-2018
    'projected': 'Ⓟ',
    'tangent': 'Ⓣ',
}


class GDTManager:
    """Manages GD&T specifications and tolerance calculations."""

    def __init__(self):
        self.spec = None

    def load_spec(self, spec: dict):
        """Load a GD&T specification from the AI engine."""
        self.spec = spec
        self._validate()

    def _validate(self):
        """Validate the GD&T specification for completeness and correctness."""
        if not self.spec:
            return

        errors = []

        # Check datums
        datums = self.spec.get('datums', [])
        datum_labels = {d['label'] for d in datums}
        if len(datums) > 0 and 'A' not in datum_labels:
            errors.append('Primary datum should be labeled A')

        # Check feature controls reference valid datums
        for fc in self.spec.get('feature_controls', []):
            for d in fc.get('datums', []):
                if d not in datum_labels:
                    errors.append(f'Feature control references unknown datum {d}')

        # Check tolerance values are reasonable
        for fc in self.spec.get('feature_controls', []):
            tol = fc.get('tolerance', 0)
            if tol <= 0:
                errors.append(f'Tolerance must be positive: {fc["feature"]}')
            if tol > 5:
                errors.append(f'Unusually large tolerance ({tol}mm): {fc["feature"]}')

        if errors:
            self.spec['_validation_warnings'] = errors

    def calculate_fit(self, nominal_mm: float, hole_class: str = 'H7',
                      shaft_class: str = 'g6') -> dict:
        """
        Calculate tolerance limits for a shaft/hole fit.

        Returns dict with upper/lower limits for both hole and shaft.
        """
        # Find the size range
        hole_table = ISO_286_HOLES.get(hole_class, {})
        shaft_table = ISO_286_SHAFTS.get(shaft_class, {})

        hole_tol_um = 0
        shaft_dev_um = (0, 0)

        for max_size in sorted(hole_table.keys()):
            if nominal_mm <= max_size:
                hole_tol_um = hole_table[max_size]
                break

        for max_size in sorted(shaft_table.keys()):
            if nominal_mm <= max_size:
                shaft_dev_um = shaft_table[max_size]
                break

        return {
            'nominal': nominal_mm,
            'hole': {
                'class': hole_class,
                'upper': nominal_mm + hole_tol_um / 1000,
                'lower': nominal_mm,
                'tolerance_mm': hole_tol_um / 1000,
            },
            'shaft': {
                'class': shaft_class,
                'upper': nominal_mm + shaft_dev_um[0] / 1000,
                'lower': nominal_mm + shaft_dev_um[1] / 1000,
                'tolerance_mm': abs(shaft_dev_um[0] - shaft_dev_um[1]) / 1000,
            },
            'min_clearance': (0 - shaft_dev_um[0]) / 1000,
            'max_clearance': (hole_tol_um - shaft_dev_um[1]) / 1000,
        }

    def get_feature_control_text(self, fc: dict) -> str:
        """
        Generate a human-readable feature control frame string.
        e.g. ⊕ ⌀0.050 Ⓜ | A | B | C
        """
        symbol = GDT_SYMBOLS.get(fc['symbol'], '?')
        tol = fc.get('tolerance', 0)

        parts = [symbol]

        if fc.get('diameter_zone'):
            parts.append(f'⌀{tol:.3f}')
        else:
            parts.append(f'{tol:.3f}')

        modifier = fc.get('mmc') and 'MMC' or fc.get('lmc') and 'LMC' or ''
        if modifier:
            parts.append(GDT_MODIFIERS[modifier])

        datum_refs = fc.get('datums', [])
        for d in datum_refs:
            parts.append(f'| {d}')

        return ' '.join(parts)

    def get_parameter_tolerances(self) -> dict:
        """
        Return a dict mapping parameter names to their tolerance info.
        Used to annotate Fusion 360 user parameters.
        """
        result = {}
        if not self.spec:
            return result

        for dt in self.spec.get('dimensional_tolerances', []):
            name = dt['feature']
            fit = dt.get('fit', '')
            result[name] = {
                'nominal': dt['nominal'],
                'upper': dt.get('upper', 0),
                'lower': dt.get('lower', 0),
                'fit': fit,
                'comment': f'[{fit}: +{dt.get("upper", 0)}/{dt.get("lower", 0)}]' if fit else ''
            }

        return result

    def get_surface_finishes(self) -> list:
        """Return surface finish requirements."""
        if not self.spec:
            return []
        return self.spec.get('surface_finishes', [])

    def get_drawing_annotations(self) -> dict:
        """
        Return all annotation data needed for drawing creation.
        Organized by annotation type for easy consumption.
        """
        if not self.spec:
            return {}

        return {
            'datums': self.spec.get('datums', []),
            'feature_controls': [
                {**fc, 'text': self.get_feature_control_text(fc)}
                for fc in self.spec.get('feature_controls', [])
            ],
            'surface_finishes': self.spec.get('surface_finishes', []),
            'dimensional_tolerances': self.spec.get('dimensional_tolerances', []),
            'general_tolerance': self.spec.get('general_tolerance', 'ISO 2768-mK'),
        }

    def to_json(self) -> str:
        """Serialize the full spec to JSON."""
        return json.dumps(self.spec, indent=2) if self.spec else '{}'

    @staticmethod
    def suggest_surface_finish(process: str) -> float:
        """Suggest Ra (μm) based on manufacturing process."""
        process_map = {
            'lapping': 0.2, 'honing': 0.4, 'grinding': 1.6,
            'turning': 3.2, 'milling': 3.2, 'drilling': 6.3,
            'reaming': 1.6, 'broaching': 1.6, 'EDM': 3.2,
            'casting': 12.5, 'forging': 12.5, 'laser_cutting': 6.3,
            '3d_printing_fdm': 12.5, '3d_printing_sla': 3.2,
            '3d_printing_sls': 12.5, '3d_printing_metal': 6.3,
        }
        return process_map.get(process.lower(), 3.2)
