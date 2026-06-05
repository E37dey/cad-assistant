"""
CAD Assistant v2 — Unified Fusion 360 Add-in
Features: 3D Model · GD&T · Drawing · Validation · Image Repair ·
          Export STEP/STL · BOM · Smart Edit · Weight Optimization · Comparison
"""

import adsk.core
import adsk.fusion
import traceback
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
import ssl
import re
import os
import sys
import time
import tempfile

# ── Path setup ──
ADDIN_DIR = os.path.dirname(os.path.realpath(__file__))
for d in (os.path.join(ADDIN_DIR, 'lib'), os.path.join(ADDIN_DIR, 'prompts')):
    if d not in sys.path:
        sys.path.insert(0, d)

# ── Globals ──
_app = None
_ui  = None
_palette = None
_handlers = []
_execute_event = None

PALETTE_ID       = 'cadAssistantPalette'
EXECUTE_EVENT_ID = 'cadAssistant_execute'

_provider       = 'claude'
_claude_api_key = ''
_ollama_model   = 'qwen2.5-coder:7b'

# Confirmation gate for interpret → build flow
_confirm_event  = threading.Event()
_confirm_result = False

OLLAMA_URL     = 'http://localhost:11434/api/generate'
CLAUDE_API_URL = 'https://api.anthropic.com/v1/messages'
CLAUDE_MODEL   = 'claude-opus-4-8'   # strongest model — best at complex CAD code, fewer retries
CLAUDE_PING    = 'claude-haiku-4-5-20251001'

GROQ_API_URL   = 'https://api.groq.com/openai/v1/chat/completions'
GROQ_MODEL     = 'llama-3.3-70b-versatile'
GEMINI_BASE_URL= 'https://generativelanguage.googleapis.com/v1beta/models'
GEMINI_MODEL   = 'gemini-2.5-pro'   # much stronger at code than 2.0-flash (free tier has tighter rate limits)

_groq_api_key   = ''
_gemini_api_key = ''


CONFIG_FILE    = os.path.join(os.path.dirname(os.path.realpath(__file__)), '.cadassist_config.json')

# ── Config persistence ──
def _load_config():
    global _claude_api_key, _provider, _ollama_model, _groq_api_key, _gemini_api_key
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            _claude_api_key  = cfg.get('api_key', '')
            _provider        = cfg.get('provider', 'claude')
            if _provider not in ('claude', 'ollama', 'groq', 'gemini'):
                _provider = 'claude'
            _ollama_model    = cfg.get('ollama_model', 'qwen2.5-coder:7b')
            _groq_api_key    = cfg.get('groq_api_key', '')
            _gemini_api_key  = cfg.get('gemini_api_key', '')
    except Exception:
        pass

def _save_config():
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({
                'api_key':        _claude_api_key,
                'provider':       _provider,
                'ollama_model':   _ollama_model,
                'groq_api_key':   _groq_api_key,
                'gemini_api_key': _gemini_api_key,
            }, f)
    except Exception:
        pass

# Conversation context (multi-turn for smart edits)
_conversation   = []   # [{'role':'user','content':str}, ...]
_last_code      = ''
_last_params    = {}
_last_comp_name     = ''
_last_timeline_mark = 0

# Threading sync
_exec_lock   = threading.Lock()
_exec_done   = threading.Event()
_exec_result = {}

# ══════════════════════════════════════════════════════════════
# MATERIAL DATABASE
# ══════════════════════════════════════════════════════════════

MATERIAL_PROPS = {
    'Steel':           {'E': 200, 'yield': 250,  'uts': 400,  'density': 7.85, 'poisson': 0.30},
    'Aluminum':        {'E':  69, 'yield': 270,  'uts': 310,  'density': 2.70, 'poisson': 0.33},
    'Stainless Steel': {'E': 193, 'yield': 310,  'uts': 620,  'density': 8.00, 'poisson': 0.28},
    'Cast Iron':       {'E': 170, 'yield': 250,  'uts': 350,  'density': 7.20, 'poisson': 0.26},
    'Brass':           {'E': 100, 'yield': 200,  'uts': 350,  'density': 8.50, 'poisson': 0.34},
    'Titanium':        {'E': 114, 'yield': 830,  'uts': 900,  'density': 4.51, 'poisson': 0.34},
    'Plastic (ABS)':   {'E':   2, 'yield':  40,  'uts':  45,  'density': 1.05, 'poisson': 0.35},
    'Nylon':           {'E':   3, 'yield':  70,  'uts':  80,  'density': 1.14, 'poisson': 0.40},
    'PLA':             {'E':   3.5,'yield': 50,  'uts':  60,  'density': 1.24, 'poisson': 0.36},
    'PETG':            {'E':   2.3,'yield': 45,  'uts':  50,  'density': 1.27, 'poisson': 0.38},
}

# ══════════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════

try:
    from prompts import MODELING_SYSTEM_PROMPT, GDT_SYSTEM_PROMPT, DRAWING_SYSTEM_PROMPT
except Exception:
    MODELING_SYSTEM_PROMPT = r"""You are an expert Fusion 360 Python API programmer.
Return ONLY a ```python def build(rootComp, config): ``` function.
ALL dimensions in CENTIMETERS. 1mm=0.1cm. Use isComputeDeferred=True/False.
Create component, add user parameters, name everything."""

    GDT_SYSTEM_PROMPT = "You are a GD&T expert per ASME Y14.5-2018. Return only JSON."
    DRAWING_SYSTEM_PROMPT = "You are a Fusion 360 Drawing API expert. Return only def create_drawing(app, component, gdt_data):"

INTERPRET_SYSTEM_PROMPT = """You are a CAD planning assistant. The user describes a part to model in Fusion 360.
Your job is to extract and clarify the build plan — NOT to write code.
Reply in the SAME LANGUAGE as the user's message (Hebrew or English).
Output a short structured summary with these fields:
- Part type
- Dimensions (convert everything to mm)
- Key features
- Material/process (if mentioned)
- Assumptions (anything you guessed or estimated)
Keep it concise — 5-10 lines maximum."""

OLLAMA_MODELING_PROMPT = r"""You are a senior mechanical engineer and Fusion 360 Python API expert.
Your job: read the part description, then output ONE ```python ... ``` code block containing def build(rootComp, config).
No explanation, no markdown outside the code block, no imports outside build().

════════════════════════════════════════
  ABSOLUTE RULES — violations crash Fusion 360
════════════════════════════════════════
1.  UNITS: ALL values in CENTIMETERS. Conversions: 1 mm = 0.1 cm | 1 inch = 2.54 cm
2.  SKETCH CURVES — always use the FULL PATH:
      sk.sketchCurves.sketchLines.addByTwoPoints(p1, p2)       ✓
      sk.sketchCurves.sketchCircles.addByCenterRadius(c, r)     ✓
      sk.sketchCurves.sketchArcs.addByCenterStartEnd(c, s, e)   ✓
      sk.sketchLines.XXX          ✗ AttributeError
      sk.sketchCircles.XXX        ✗ AttributeError
      sk.sketchCurves.addXXX()    ✗ sketchCurves is a CONTAINER, never call .add directly
3.  isComputeDeferred: set True BEFORE adding curves, False BEFORE reading .profiles
4.  COMPONENT: use comp = rootComp directly. NEVER call addNewComponent (crashes).
5.  COMPONENT NAME: NEVER set comp.name / rootComp.name (crashes immediately).
6.  PARAMETERS: names = ASCII letters/digits/underscores only, must start with letter.
      GOOD: 'wall_t', 'bore_dia'   BAD: 'wall thickness', '6mm', 'bore-dia'
7.  config IS ALWAYS {}. Define ALL dimensions as local Python variables.
8.  FORBIDDEN PATTERNS:
      - profile.union() / profiles.union()  → draw ring as 2 concentric circles in 1 sketch
      - ObjectCollection for profiles        → use .profiles.item(N) directly
      - sketchPolygon                        → use sketchLines.addByTwoPoints in a loop
      - ThreadFeatureInputParameters         → use threadFeatures.createInput(face, True)
      - addNewComponent / addNewComponentCopy → use comp = rootComp
9.  MULTI-BODY (bolt head + shaft, etc): first extrusion = NewBodyFeatureOperation,
    every subsequent extrusion into same body = JoinFeatureOperation.
10. RETURN always: {'bodies': [body.name, ...], 'params': {}, 'features': [], 'component': comp}

════════════════════════════════════════
  PATTERN LIBRARY — copy-paste these exactly
════════════════════════════════════════

## Box (W × D × H cm)
```python
sk = comp.sketches.add(comp.xYConstructionPlane)
sk.isComputeDeferred = True
ln = sk.sketchCurves.sketchLines
ln.addByTwoPoints(adsk.core.Point3D.create(-W/2,-D/2,0), adsk.core.Point3D.create( W/2,-D/2,0))
ln.addByTwoPoints(adsk.core.Point3D.create( W/2,-D/2,0), adsk.core.Point3D.create( W/2, D/2,0))
ln.addByTwoPoints(adsk.core.Point3D.create( W/2, D/2,0), adsk.core.Point3D.create(-W/2, D/2,0))
ln.addByTwoPoints(adsk.core.Point3D.create(-W/2, D/2,0), adsk.core.Point3D.create(-W/2,-D/2,0))
sk.isComputeDeferred = False
prof = sk.profiles.item(0)
ext_in = comp.features.extrudeFeatures.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(H))
body = comp.features.extrudeFeatures.add(ext_in).bodies.item(0)
body.name = 'Box'
```

## Cylinder (radius R, height H)
```python
sk = comp.sketches.add(comp.xYConstructionPlane)
sk.isComputeDeferred = True
sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(0,0,0), R)
sk.isComputeDeferred = False
prof = sk.profiles.item(0)
ext_in = comp.features.extrudeFeatures.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(H))
body = comp.features.extrudeFeatures.add(ext_in).bodies.item(0)
body.name = 'Cylinder'
```

## Hollow Tube (outer radius R_out, inner radius R_in, height H)
```python
sk = comp.sketches.add(comp.xYConstructionPlane)
sk.isComputeDeferred = True
circles = sk.sketchCurves.sketchCircles
circles.addByCenterRadius(adsk.core.Point3D.create(0,0,0), R_out)
circles.addByCenterRadius(adsk.core.Point3D.create(0,0,0), R_in)
sk.isComputeDeferred = False
# profiles.item(0) is automatically the annular ring — no union needed
prof = sk.profiles.item(0)
ext_in = comp.features.extrudeFeatures.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(H))
body = comp.features.extrudeFeatures.add(ext_in).bodies.item(0)
body.name = 'Tube'
```

## Polygon / Hexagon (n sides, circumradius R, height H)
```python
sk = comp.sketches.add(comp.xYConstructionPlane)
sk.isComputeDeferred = True
ln = sk.sketchCurves.sketchLines
n = 6  # number of sides
pts = [adsk.core.Point3D.create(R*math.cos(2*math.pi*i/n), R*math.sin(2*math.pi*i/n), 0) for i in range(n)]
for i in range(n):
    ln.addByTwoPoints(pts[i], pts[(i+1) % n])
sk.isComputeDeferred = False
prof = sk.profiles.item(0)
ext_in = comp.features.extrudeFeatures.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(H))
body = comp.features.extrudeFeatures.add(ext_in).bodies.item(0)
```

## Cut hole through existing body (radius R, depth D — use large D to guarantee through-cut)
```python
sk_hole = comp.sketches.add(comp.xYConstructionPlane)
sk_hole.isComputeDeferred = True
sk_hole.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(cx, cy, 0), R)
sk_hole.isComputeDeferred = False
prof_hole = sk_hole.profiles.item(0)
cut_in = comp.features.extrudeFeatures.createInput(prof_hole, adsk.fusion.FeatureOperations.CutFeatureOperation)
cut_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-D * 2))
comp.features.extrudeFeatures.add(cut_in)
```

## Fillet all edges (radius FR)
```python
fillet_input = comp.features.filletFeatures.createInput()
edges = adsk.core.ObjectCollection.create()
for b in [body]:
    for e in b.edges:
        edges.add(e)
fillet_input.addConstantRadiusEdgeSet(edges, adsk.core.ValueInput.createByReal(FR), True)
comp.features.filletFeatures.add(fillet_input)
```

## Chamfer all edges (distance CH)
```python
chamfer_edges = adsk.core.ObjectCollection.create()
for e in body.edges:
    chamfer_edges.add(e)
ch_input = comp.features.chamferFeatures.createInput(chamfer_edges, True)
ch_input.setToEqualDistance(adsk.core.ValueInput.createByReal(CH))
comp.features.chamferFeatures.add(ch_input)
```

## Shell (hollow out, wall thickness T — remove top face)
```python
faces_to_remove = adsk.core.ObjectCollection.create()
# Find top face (highest Z)
top_face = max(body.faces, key=lambda f: f.centroid.z)
faces_to_remove.add(top_face)
shell_in = comp.features.shellFeatures.createInput(faces_to_remove, False)
shell_in.insideThickness = adsk.core.ValueInput.createByReal(T)
comp.features.shellFeatures.add(shell_in)
```

## Thread on cylindrical face (M-size, full length)
```python
# Get the cylindrical face of the shaft (face with largest area that is cylindrical)
cyl_face = None
for face in body.faces:
    if face.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType:
        cyl_face = face
        break
if cyl_face:
    thread_in = comp.features.threadFeatures.createInput(cyl_face, True)
    thread_in.isModeled = True
    comp.features.threadFeatures.add(thread_in)
```

## Rectangular pattern (NX × NY copies, spacing SX × SY)
```python
bodies_col = adsk.core.ObjectCollection.create()
bodies_col.add(body)
x_dir = adsk.core.InfiniteLine3D.create(adsk.core.Point3D.create(0,0,0), adsk.core.Vector3D.create(1,0,0))
y_dir = adsk.core.InfiniteLine3D.create(adsk.core.Point3D.create(0,0,0), adsk.core.Vector3D.create(0,1,0))
pat_in = comp.features.rectangularPatternFeatures.createInput(
    bodies_col,
    x_dir, adsk.core.ValueInput.createByReal(NX), adsk.core.ValueInput.createByReal(SX),
    adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
pat_in.setDirectionTwo(y_dir, adsk.core.ValueInput.createByReal(NY), adsk.core.ValueInput.createByReal(SY))
comp.features.rectangularPatternFeatures.add(pat_in)
```

## Circular pattern (N copies around Z axis)
```python
bodies_col = adsk.core.ObjectCollection.create()
bodies_col.add(body)
z_axis = comp.zConstructionAxis
pat_in = comp.features.circularPatternFeatures.createInput(bodies_col, z_axis)
pat_in.quantity = adsk.core.ValueInput.createByReal(N)
pat_in.totalAngle = adsk.core.ValueInput.createByString('360 deg')
pat_in.isSymmetric = False
comp.features.circularPatternFeatures.add(pat_in)
```

## Flat rectangular frame (outer W×L, beam section S, height H) — TWO nested rectangles in ONE sketch
```python
sk = comp.sketches.add(comp.xYConstructionPlane)
sk.isComputeDeferred = True
ln = sk.sketchCurves.sketchLines
# Outer rectangle
ln.addByTwoPoints(adsk.core.Point3D.create(0,   0,   0), adsk.core.Point3D.create(W,   0,   0))
ln.addByTwoPoints(adsk.core.Point3D.create(W,   0,   0), adsk.core.Point3D.create(W,   L,   0))
ln.addByTwoPoints(adsk.core.Point3D.create(W,   L,   0), adsk.core.Point3D.create(0,   L,   0))
ln.addByTwoPoints(adsk.core.Point3D.create(0,   L,   0), adsk.core.Point3D.create(0,   0,   0))
# Inner rectangle (creates the frame ring profile automatically)
ln.addByTwoPoints(adsk.core.Point3D.create(S,   S,   0), adsk.core.Point3D.create(W-S, S,   0))
ln.addByTwoPoints(adsk.core.Point3D.create(W-S, S,   0), adsk.core.Point3D.create(W-S, L-S, 0))
ln.addByTwoPoints(adsk.core.Point3D.create(W-S, L-S, 0), adsk.core.Point3D.create(S,   L-S, 0))
ln.addByTwoPoints(adsk.core.Point3D.create(S,   L-S, 0), adsk.core.Point3D.create(S,   S,   0))
sk.isComputeDeferred = False
# ALWAYS use index 1 — it is the frame ring (ring between outer and inner rectangle)
prof = sk.profiles.item(1)
ext_in = comp.features.extrudeFeatures.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(H))
body = comp.features.extrudeFeatures.add(ext_in).bodies.item(0)
body.name = 'Frame'
```

## Mirror about XZ plane
```python
bodies_col = adsk.core.ObjectCollection.create()
bodies_col.add(body)
mirror_in = comp.features.mirrorFeatures.createInput(bodies_col, comp.xZConstructionPlane)
comp.features.mirrorFeatures.add(mirror_in)
```

## Bolt pattern (N holes on bolt circle diameter BCD, hole radius HR)
```python
sk_bolt = comp.sketches.add(comp.xYConstructionPlane)
sk_bolt.isComputeDeferred = True
circles_bolt = sk_bolt.sketchCurves.sketchCircles
for i in range(N):
    angle = 2 * math.pi * i / N
    cx = (BCD/2) * math.cos(angle)
    cy = (BCD/2) * math.sin(angle)
    circles_bolt.addByCenterRadius(adsk.core.Point3D.create(cx, cy, 0), HR)
sk_bolt.isComputeDeferred = False
for i in range(sk_bolt.profiles.count):
    prof_b = sk_bolt.profiles.item(i)
    cut_in = comp.features.extrudeFeatures.createInput(prof_b, adsk.fusion.FeatureOperations.CutFeatureOperation)
    cut_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-PART_HEIGHT * 2))
    comp.features.extrudeFeatures.add(cut_in)
```

════════════════════════════════════════
  COMPLETE EXAMPLE — Flanged Bushing
════════════════════════════════════════
```python
def build(rootComp, config):
    import adsk.core, adsk.fusion, math
    design = rootComp.parentDesign
    comp   = rootComp

    # Parameters (all in cm)
    od        = 4.0    # outer diameter 40 mm
    bore      = 2.0    # bore diameter 20 mm
    height    = 6.0    # bushing height 60 mm
    flange_od = 6.0    # flange outer diameter 60 mm
    flange_h  = 0.8    # flange thickness 8 mm
    fillet_r  = 0.1    # fillet radius 1 mm

    design.userParameters.add('outer_dia',  adsk.core.ValueInput.createByReal(od),       'cm', 'Outer diameter')
    design.userParameters.add('bore_dia',   adsk.core.ValueInput.createByReal(bore),     'cm', 'Bore diameter')
    design.userParameters.add('bush_height',adsk.core.ValueInput.createByReal(height),   'cm', 'Bushing height')
    design.userParameters.add('flange_od',  adsk.core.ValueInput.createByReal(flange_od),'cm', 'Flange OD')
    design.userParameters.add('flange_h',   adsk.core.ValueInput.createByReal(flange_h), 'cm', 'Flange thickness')

    # --- Bushing body (hollow cylinder) ---
    sk1 = comp.sketches.add(comp.xYConstructionPlane)
    sk1.isComputeDeferred = True
    circles1 = sk1.sketchCurves.sketchCircles
    circles1.addByCenterRadius(adsk.core.Point3D.create(0,0,0), od/2)
    circles1.addByCenterRadius(adsk.core.Point3D.create(0,0,0), bore/2)
    sk1.isComputeDeferred = False
    prof1 = sk1.profiles.item(0)
    ext1 = comp.features.extrudeFeatures.createInput(prof1, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext1.setDistanceExtent(False, adsk.core.ValueInput.createByReal(height))
    body = comp.features.extrudeFeatures.add(ext1).bodies.item(0)
    body.name = 'Bushing'

    # --- Flange (joined to bushing) ---
    sk2 = comp.sketches.add(comp.xYConstructionPlane)
    sk2.isComputeDeferred = True
    circles2 = sk2.sketchCurves.sketchCircles
    circles2.addByCenterRadius(adsk.core.Point3D.create(0,0,0), flange_od/2)
    circles2.addByCenterRadius(adsk.core.Point3D.create(0,0,0), od/2)
    sk2.isComputeDeferred = False
    prof2 = sk2.profiles.item(0)
    ext2 = comp.features.extrudeFeatures.createInput(prof2, adsk.fusion.FeatureOperations.JoinFeatureOperation)
    ext2.setDistanceExtent(False, adsk.core.ValueInput.createByReal(flange_h))
    comp.features.extrudeFeatures.add(ext2)

    # --- Fillet top edge of bushing ---
    fillet_input = comp.features.filletFeatures.createInput()
    fillet_edges = adsk.core.ObjectCollection.create()
    top_z = height
    for face in body.faces:
        if abs(face.centroid.z - top_z) < 0.05:
            for edge in face.edges:
                fillet_edges.add(edge)
    if fillet_edges.count > 0:
        fillet_input.addConstantRadiusEdgeSet(fillet_edges, adsk.core.ValueInput.createByReal(fillet_r), True)
        try:
            comp.features.filletFeatures.add(fillet_input)
        except Exception:
            pass  # fillet is cosmetic, continue if it fails

    return {
        'bodies': [body.name],
        'params': {'outer_dia': od, 'bore_dia': bore, 'bush_height': height},
        'features': ['extrude_bushing', 'extrude_flange', 'fillet_top'],
        'component': comp
    }
```

Now write the build() function for the described part. Return ONLY the ```python ... ``` block.
"""

QUERY_SYSTEM_PROMPT = """You are a Fusion 360 Python API expert. The user asks a question about the current model.

You receive a JSON model context. Write a Python function:
def query(design, rootComp):
    ...
    return answer_string  # must be a plain str in Hebrew

Rules:
- Read-only ONLY. Never call .add(), .deleteMe(), extrudeFeatures, etc.
- ALL internal values in cm; convert to mm when showing to user (multiply by 10).
- physicalProperties may be slow — call body.physicalProperties and guard with try/except.
- Return a short human-readable string in Hebrew.
- Import only: adsk, adsk.core, adsk.fusion, math, json
Return ONLY a ```python ... ``` block containing def query(design, rootComp):"""

EDIT_SYSTEM_PROMPT = r"""You are a Fusion 360 Python API expert. The user wants to MODIFY an existing model.

You receive model_context JSON and an edit request. Write:
def modify(design, rootComp, timeline_mark):
    ...
    return {'ok': True, 'description': 'what was done'}

CRITICAL RULES:
1. ALL dimensions in CENTIMETERS (mm÷10).
2. Access existing bodies via rootComp.bRepBodies.item(0) or search by name.
3. Never delete or suppress existing features.
4. Wrap risky calls in try/except; return {'ok': False, 'error': '...'} on failure.
5. For fillet: collect edges into an ObjectCollection, use rootComp.features.filletFeatures.createInput(), add edges with addConstantRadiusEdgeSet().
6. For shell: use rootComp.features.shellFeatures.createInput(faces_to_remove, inside_thickness).
7. For pattern: use rectangularPatternFeatures or circularPatternFeatures.
8. To change a parameter: design.userParameters.itemByName(name).expression = '5.0'
9. Return {'ok': True, 'description': 'short Hebrew description of what was done'}
Return ONLY a ```python ... ``` block containing def modify(design, rootComp, timeline_mark):"""

PARAM_PARSE_PROMPT = """You are a Fusion 360 parameter assistant. Parse the user's request into JSON.

Return ONLY a valid JSON object (no markdown, no explanation):
{"action": "list"}
OR
{"action": "set", "name": "ValidParamName", "expression": "0.25", "unit": "cm", "comment": ""}

Rules:
- "action" is "list" or "set"
- For "set": convert mm to cm (divide by 10) in the expression
- "name" must be a valid identifier (letters/digits/underscore, start with letter)
- Examples:
  "רשום פרמטרים" → {"action": "list"}
  "צור WallThickness = 2.5mm" → {"action": "set", "name": "WallThickness", "expression": "0.25", "unit": "cm", "comment": ""}
  "שנה height ל-50mm" → {"action": "set", "name": "height", "expression": "5.0", "unit": "cm", "comment": ""}"""

MANUFACTURING_PROMPT = """You are a manufacturing engineer expert. Analyze the given Fusion 360 model data for the specified process.

Respond in Hebrew. Be specific and practical. Reference body names and dimensions from the context.

For CNC Machining: flag undercuts, thin walls (<1mm), deep pockets, sharp internal corners. Suggest tool sizes.
For 3D Printing (FDM): check overhangs >45°, wall thickness, bridging. Suggest orientation and supports.
For Sheet Metal: check bend radii vs thickness. Flag impossible geometries.
For Injection Molding: check draft angles, wall uniformity, sink marks risk.

Format with numbered lists. Be concise."""

GROQ_MODELING_PROMPT = r"""You are a Fusion 360 Python API expert. Output ONLY a python code block, nothing else.

CRITICAL RULES — violations will crash the program:
1. ALL dimensions in CENTIMETERS. mm÷10=cm. Example: 40mm → 4.0, 80mm → 8.0
2. Always set sketch.isComputeDeferred=True BEFORE adding curves, then set to False BEFORE accessing .profiles
3. Create a component first — NEVER build on rootComp directly
4. Every sketch needs ONE plane. Use: comp.xYConstructionPlane / xZConstructionPlane / yZConstructionPlane
5. Return the result dict
6. FORBIDDEN — crashes immediately:
   - rootComp.name = anything  ← CRASH: root component name cannot be changed
   - sketchCurves.sketchPolygon  ← does NOT exist
   - sketchCurves.sketchRectangles.addByTwoPoints  ← does NOT exist
   Use sk.sketchCurves.sketchLines.addByTwoPoints() for all straight edges.
   Only set comp.name AFTER: occ = rootComp.occurrences.addNewComponent(...); comp = occ.component
7. userParameters.add() — name MUST be a valid identifier:
   - Only letters, digits, underscores. Must start with a letter.
   - GOOD: 'bolt_length', 'diameter', 'hex_width'
   - BAD: 'M6 length', '6mm', 'bolt-length', 'length (mm)'  ← all crash

DRAWING POLYGONS — use this pattern for any n-sided polygon (hexagon, square, etc.):
```python
import math
lines = sk.sketchCurves.sketchLines
n = 6          # number of sides
r = 0.5        # circumradius in cm
pts = [adsk.core.Point3D.create(r*math.cos(2*math.pi*i/n), r*math.sin(2*math.pi*i/n), 0) for i in range(n)]
for i in range(n):
    lines.addByTwoPoints(pts[i], pts[(i+1) % n])
```

DRAWING RECTANGLES — use this pattern:
```python
lines = sk.sketchCurves.sketchLines
lines.addByTwoPoints(adsk.core.Point3D.create(-w/2, -h/2, 0), adsk.core.Point3D.create( w/2, -h/2, 0))
lines.addByTwoPoints(adsk.core.Point3D.create( w/2, -h/2, 0), adsk.core.Point3D.create( w/2,  h/2, 0))
lines.addByTwoPoints(adsk.core.Point3D.create( w/2,  h/2, 0), adsk.core.Point3D.create(-w/2,  h/2, 0))
lines.addByTwoPoints(adsk.core.Point3D.create(-w/2,  h/2, 0), adsk.core.Point3D.create(-w/2, -h/2, 0))
```

TEMPLATE — follow this structure exactly:

```python
def build(rootComp, config):
    import adsk.core, adsk.fusion, math
    design = rootComp.parentDesign

    # IMPORTANT: use rootComp directly — do NOT call addNewComponent (crashes in Part documents)
    comp = rootComp

    # Parameters (all values in cm)
    design.userParameters.add('height', adsk.core.ValueInput.createByReal(5.0), 'cm', '')

    # Sketch on XY plane
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.isComputeDeferred = True
    circles = sk.sketchCurves.sketchCircles
    circles.addByCenterRadius(adsk.core.Point3D.create(0, 0, 0), 4.0)
    sk.isComputeDeferred = False

    # Extrude
    prof = sk.profiles.item(0)
    ext_in = comp.features.extrudeFeatures.createInput(
        prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext_in.setDistanceExtent(False, adsk.core.ValueInput.createByReal(5.0))
    body = comp.features.extrudeFeatures.add(ext_in).bodies.item(0)
    body.name = 'MainBody'

    return {'bodies': [body.name], 'params': {'height': 5.0}, 'features': [], 'component': comp}
```

Now write the build() function for the described part. Return ONLY the ```python ... ``` block."""

IMAGE_REPAIR_PROMPT = r"""You are a mechanical engineer expert in reverse engineering and FDM 3D printing.
The user shows you a broken or damaged part that needs to be replaced using a 3D printer.

YOUR TASK:
1. IDENTIFY the part — what is it? what is its function?
2. ESTIMATE dimensions — use visible reference objects, standard sizes, or typical proportions
3. ANALYZE the break — where/how did it fail?
4. DESIGN a replacement OPTIMIZED for FDM 3D printing:
   - Wall thickness: minimum 2mm, ideally 3mm for load-bearing areas
   - No unsupported overhangs > 45° where avoidable
   - Add fillets (min 1.5mm) for stress relief at corners
   - Consider print orientation: lay flat for best layer adhesion
   - Slightly oversize critical dimensions by 0.2–0.5mm for fit tolerance
5. GENERATE Fusion 360 Python build() function

FORMAT YOUR RESPONSE:
## Part Analysis
[Identify part, function, estimated dimensions, failure analysis]

## Print Settings Recommendation
[Layer height, infill %, supports needed, print orientation]

```python
def build(rootComp, config):
    import adsk.core, adsk.fusion, math
    # All dims in CM
    ...
    return {"bodies": [], "params": {}, "features": []}
```

Be conservative with dimensions — slightly stronger is better than too weak.
If you cannot determine a dimension, state your assumption clearly."""

EXPAND_SYSTEM_PROMPT = """You are a senior mechanical design engineer. The user gives a SHORT request.
Expand it into a COMPLETE, detailed, buildable engineering specification — the kind a junior
engineer could model with zero further questions. Be thorough: a vague request must become a
full spec. Pick sensible standard/industry values for anything unspecified.

Structure the spec with these sections (omit a section only if truly irrelevant):
1. PURPOSE / design intent — what the part does and what it mates with (motor, bearing, shaft, bolts...).
2. OVERALL dimensions (mm) — length, width, height/thickness, key diameters.
3. FEATURES — list each feature explicitly with numbers: base shape; holes (dia, count, spacing,
   bolt-circle, counterbore/countersink); bores + shoulders; slots; ribs/gussets (thickness);
   bosses; pockets; keyways; threads; patterns; mounting points.
4. FILLETS & CHAMFERS — radii (internal fillets R2-R3 for CNC, edge chamfers 0.5 mm).
5. CLEARANCES / FITS / TOLERANCES — for any mating or moving interface (0.1-0.3 mm, H7, etc.).
6. WALL THICKNESS — minimum wall consistent with the material/process.
7. For ASSEMBLIES — list every component, its dimensions, how parts mate, the shared axis,
   and any motion (which part rotates/slides and about which axis).
8. MATERIAL & PROCESS notes if implied.

Rules:
- KEEP every explicit value the user gave; only ADD missing detail, never contradict them.
- Be specific and numeric everywhere. Real engineering values, no placeholders, no questions.
- Output ONLY the specification (clear bulleted sections), in the SAME language the user used."""


WEIGHT_OPT_PROMPT = """You are a mechanical engineer specializing in lightweight design.
Given a part's parameters and material, suggest specific changes to reduce weight while maintaining structural integrity.
Focus on: adding pockets/lightening holes, shell operations, rib reinforcement instead of solid walls.
Return a numbered list of specific suggestions with estimated weight savings percentage."""

EDIT_KEYWORDS_HE = ['תוסיף', 'הסר', 'שנה', 'עדכן', 'תגדיל', 'תקטין', 'תעבה', 'תדקן', 'הוסף',
                     'תוריד', 'תעלה', 'תרחיב', 'תצמצם', 'תאריך', 'תקצר']
EDIT_KEYWORDS_EN = ['add', 'remove', 'change', 'modify', 'make it', 'update', 'increase',
                    'decrease', 'also', 'instead', 'replace', 'adjust', 'fix', 'move',
                    'rotate', 'mirror', 'scale', 'reduce', 'enlarge', 'thicken', 'thin']
RESET_KEYWORDS   = ['חדש', 'new', 'התחל', 'start', 'reset', 'חלק חדש', 'מחדש', 'clear']

# ══════════════════════════════════════════════════════════════
# AI CALLS
# ══════════════════════════════════════════════════════════════

def _call_claude(system, messages, max_tokens=8192):
    """Call Claude API with multi-turn messages list.

    NOTE: Opus 4.8 DEPRECATED the `temperature` parameter and rejects it with a
    400 error — so we do not send it (the model handles sampling internally and
    is effectively deterministic for code). The system prompt is sent as a
    cache_control block so repeated calls (retries, successive builds) reuse it
    instead of re-processing it — faster and cheaper.
    """
    payload = json.dumps({
        'model': CLAUDE_MODEL,
        'max_tokens': max_tokens,
        'system': [{'type': 'text', 'text': system,
                    'cache_control': {'type': 'ephemeral'}}],
        'messages': messages,
    }).encode('utf-8')

    req = urllib.request.Request(
        CLAUDE_API_URL, data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': _claude_api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Claude API {e.code}: {e.read().decode("utf-8", errors="replace")}')

    parts = [b['text'] for b in body.get('content', []) if b.get('type') == 'text']
    return '\n'.join(parts)


def _call_claude_vision(image_b64, media_type, user_text, system, max_tokens=8192):
    """Call Claude API with an image."""
    payload = json.dumps({
        'model': CLAUDE_MODEL,
        'max_tokens': max_tokens,
        'system': system,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {
                    'type': 'base64',
                    'media_type': media_type,
                    'data': image_b64,
                }},
                {'type': 'text', 'text': user_text},
            ]
        }]
    }).encode('utf-8')

    req = urllib.request.Request(
        CLAUDE_API_URL, data=payload,
        headers={
            'Content-Type': 'application/json',
            'x-api-key': _claude_api_key,
            'anthropic-version': '2023-06-01',
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'Claude Vision API {e.code}: {e.read().decode("utf-8", errors="replace")}')

    parts = [b['text'] for b in body.get('content', []) if b.get('type') == 'text']
    return '\n'.join(parts)


def _http_post(url, payload_bytes, headers):
    """POST with system proxy + SSL context that works inside Fusion 360."""
    ssl_ctx = ssl.create_default_context()
    proxy_handler = urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx),
        proxy_handler,
    )
    req = urllib.request.Request(url, data=payload_bytes, headers=headers, method='POST')
    with opener.open(req, timeout=120) as resp:
        return resp.read().decode('utf-8')


def _call_groq(system, messages, max_tokens=8192):
    """Call Groq API (OpenAI-compatible, free tier)."""
    # Convert our message format to OpenAI format
    msgs = [{'role': 'system', 'content': system}] + messages
    payload = json.dumps({
        'model': GROQ_MODEL,
        'max_tokens': max_tokens,
        'messages': msgs,
        'temperature': 0.2,
    }).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {_groq_api_key}',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    try:
        raw = _http_post(GROQ_API_URL, payload, headers)
        body = json.loads(raw)
        choices = body.get('choices')
        if not choices:
            # Error payloads can arrive with HTTP 200 and no 'choices'.
            raise RuntimeError(f'Groq returned no choices: {body.get("error", body)}')
        return choices[0]['message']['content']
    except urllib.error.HTTPError as e:
        err = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Groq error {e.code}: {err[:300]}')


def _call_gemini(system, messages, max_tokens=8192):
    """Call Google Gemini API (free tier)."""
    contents = []
    for m in messages:
        role = 'user' if m['role'] == 'user' else 'model'
        contents.append({'role': role, 'parts': [{'text': m['content']}]})

    payload = json.dumps({
        'system_instruction': {'parts': [{'text': system}]},
        'contents': contents,
        'generationConfig': {'maxOutputTokens': max_tokens, 'temperature': 0.2},
    }).encode('utf-8')

    url = GEMINI_BASE_URL + '/' + GEMINI_MODEL + ':generateContent?key=' + _gemini_api_key

    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            # A safety-blocked or truncated response has no usable parts —
            # surface the reason instead of crashing with a KeyError.
            candidates = body.get('candidates')
            if not candidates:
                reason = body.get('promptFeedback', {}).get('blockReason', 'no candidates')
                raise RuntimeError(f'Gemini returned no output ({reason})')
            parts = candidates[0].get('content', {}).get('parts')
            if not parts:
                reason = candidates[0].get('finishReason', 'no parts')
                raise RuntimeError(f'Gemini returned no text ({reason})')
            return parts[0].get('text', '')
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='replace')
            if e.code == 429 and attempt < 3:
                wait = 20 * (attempt + 1)
                _send('system', f'Gemini rate limit — ממתין {wait} שניות ({attempt+1}/3)...')
                time.sleep(wait)
                continue
            raise RuntimeError(f'Gemini API {e.code}: {err[:200]}')


def _call_ollama(user_msg, model=None, max_tokens=4096, system=None):
    """Call local Ollama — system and prompt sent as separate fields for better adherence."""
    effective_system = system if system else OLLAMA_MODELING_PROMPT
    payload_dict = {
        'model':  model or _ollama_model,
        'system': effective_system,
        'prompt': user_msg,
        'stream': False,
        'options': {'temperature': 0.1, 'num_predict': 4096},
    }
    payload = json.dumps(payload_dict).encode('utf-8')
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode('utf-8')).get('response', '')
    except urllib.error.URLError as e:
        raise RuntimeError(f'Ollama: {e.reason}')


def _call_ai(system, messages, max_tokens=8192):
    """Route to the active AI provider."""
    if _provider == 'claude':
        return _call_claude(system, messages, max_tokens)
    elif _provider == 'groq':
        return _call_groq(system, messages, max_tokens)
    elif _provider == 'gemini':
        return _call_gemini(system, messages, max_tokens)
    else:  # ollama
        return _call_ollama(messages[-1]['content'] if messages else '')


def _simple_call(system, user_msg, max_tokens=8192):
    """Single-turn call for GD&T, drawing, weight opt."""
    return _call_ai(system, [{'role': 'user', 'content': user_msg}], max_tokens)


# ══════════════════════════════════════════════════════════════
# CODE EXTRACTION
# ══════════════════════════════════════════════════════════════

def _extract_python(text, marker=''):
    blocks = re.findall(r'```python\s*\n(.*?)```', text, re.DOTALL)
    if marker:
        for b in blocks:
            if marker in b:
                return b.strip()
    for b in blocks:
        if 'def build(' in b or 'def create_drawing(' in b:
            return b.strip()
    if not blocks and 'def build(' in text:
        return text.strip()
    return blocks[0].strip() if blocks else None


def _extract_json(text):
    m = re.search(r'```json\s*\n(.*?)```', text, re.DOTALL)
    if m:
        lines = [l for l in m.group(1).strip().split('\n') if not l.strip().startswith('//')]
        try:
            return json.loads('\n'.join(lines))
        except Exception:
            pass
    return None

# ══════════════════════════════════════════════════════════════
# SANDBOX EXECUTOR
# ══════════════════════════════════════════════════════════════

# Word-boundary patterns so a blocked builtin like open()/eval()/socket is
# caught, but legitimate method calls (file.open(, reopen(, mysocket) are not
# falsely rejected. Python is case-sensitive, so we match case-sensitively.
_BLOCKED_PATTERNS = [re.compile(p) for p in (
    r'\bos\.system\b', r'\bsubprocess\b', r'\bshutil\.rmtree\b',
    r'(?<![\w.])eval\s*\(', r'\b__import__\b', r'(?<![\w.])open\s*\(',
    r'(?<![\w.])socket\b', r'\bapp\.quit\b', r'\bsys\.exit\b', r'\bctypes\b',
)]

_ALLOWED_MODULES = {
    'adsk', 'adsk.core', 'adsk.fusion', 'adsk.cam',
    'adsk.drawing', 'math', 'collections', 'json', 're',
    'itertools', 'functools',
    # Safe, pure-computation/formatting stdlib modules that generated code
    # (especially drawings) commonly imports. No I/O, network, or system access,
    # so allowing them doesn't weaken the sandbox against os/subprocess/socket.
    'datetime', 'time', 'traceback', 'random', 'string', 'copy',
    'decimal', 'fractions',
}


def _validate_code(code):
    for pat in _BLOCKED_PATTERNS:
        m = pat.search(code)
        if m:
            return False, f'Blocked: {m.group(0)}'
    if len(code) > 120_000:
        return False, 'Code too large'
    return True, ''


def _make_namespace():
    import math as _m
    def _safe_import(name, *a, **kw):
        base = name.split('.')[0]
        if base in _ALLOWED_MODULES or name in _ALLOWED_MODULES:
            return __import__(name, *a, **kw)
        raise ImportError(f'Module "{name}" not allowed')

    return {
        '__builtins__': {
            'range': range, 'len': len, 'int': int, 'float': float,
            'str': str, 'bool': bool, 'list': list, 'dict': dict,
            'tuple': tuple, 'set': set, 'enumerate': enumerate,
            'zip': zip, 'map': map, 'filter': filter, 'sorted': sorted,
            'min': min, 'max': max, 'abs': abs, 'round': round,
            'sum': sum, 'any': any, 'all': all, 'isinstance': isinstance,
            'type': type, 'hasattr': hasattr, 'getattr': getattr,
            'setattr': setattr, 'print': lambda *a, **kw: None,
            'True': True, 'False': False, 'None': None,
            '__import__': _safe_import,
            'ValueError': ValueError, 'TypeError': TypeError,
            'RuntimeError': RuntimeError, 'KeyError': KeyError,
            'IndexError': IndexError, 'AttributeError': AttributeError,
            'Exception': Exception, 'math': _m,
        }
    }


def _sanitize_param_name(name: str) -> str:
    """Convert any string to a valid Fusion 360 parameter name (ASCII only)."""
    import re as _re
    # Replace spaces/hyphens with underscore
    name = _re.sub(r'[\s\-]+', '_', name)
    # Keep only ASCII letters, digits, underscore
    name = _re.sub(r'[^a-zA-Z0-9_]', '', name)
    # Must start with a letter
    if name and not name[0].isalpha():
        name = 'p_' + name
    return name or 'param'


def _lint_build_code(code: str) -> list:
    """Detect known bad API patterns before execution. Returns list of issue strings."""
    import re as _re
    issues = []
    checks = [
        (r'\.union\(',          "profile.union() doesn't exist — draw ring as 2 concentric circles in 1 sketch"),
        (r'sketchPolygon',      "sketchPolygon doesn't exist — use sk.sketchCurves.sketchLines.addByTwoPoints for polygons"),
        (r'\b(rootComp|comp|root|component)\.name\s*=', "Component .name cannot be changed (rootComp/comp/root.name=) — remove that line"),
        (r'addNewComponent',    "addNewComponent crashes in Part documents — use comp=rootComp directly"),
        (r'ObjectCollection',   "ObjectCollection is not needed — select profiles directly via .profiles.item()"),
        (r'ThreadFeatureInputParameters', "ThreadFeatureInputParameters doesn't exist — use comp.features.threadFeatures.createInput(face, True). (threadFeatures + ThreadFeatureInput ARE valid.)"),
        (r'config\[',            "Do NOT read from config[] — config is always {}. Define all dimensions as local variables or userParameters directly in build()"),
        (r'ThroughAllExtentDefinition.*Cut|CutFeatureOperation.*ThroughAll', "ThroughAll cuts fail if direction is wrong — use setDistanceExtent with large negative value instead"),
        (r'(?<!sketchCurves)\.sketchLines\b', "sketch.sketchLines is WRONG — must be sketch.sketchCurves.sketchLines (will auto-fix)"),
        (r'(?<!sketchCurves)\.sketchCircles\b', "sketch.sketchCircles is WRONG — must be sketch.sketchCurves.sketchCircles (will auto-fix)"),
        (r'(?<!sketchCurves)\.sketchArcs\b', "sketch.sketchArcs is WRONG — must be sketch.sketchCurves.sketchArcs (will auto-fix)"),
        (r'\.sketchCurves\.(addByTwoPoints|addTwoPoint|addByCenterRadius|addByCenterStartEnd)', "sketchCurves.addX() is WRONG — must go through .sketchLines/.sketchCircles/.sketchArcs first (will auto-fix)"),
        (r'\.profiles\.item\((\d+)\)',  None),  # check index bounds later
    ]
    for pattern, msg in checks:
        if msg and _re.search(pattern, code):
            issues.append(msg)
    # Syntax check
    try:
        compile(code, '<lint>', 'exec')
    except SyntaxError as e:
        lines = code.splitlines()
        lineno = e.lineno or 0
        ctx = lines[lineno-1].strip() if 0 < lineno <= len(lines) else ''
        issues.append(f"SyntaxError line {lineno}: {e.msg} — {ctx}")
    return issues


def _sanitize_build_code(code: str) -> str:
    """Fix known AI code mistakes before execution."""
    import re as _re

    _used_param_names = set()
    safe_lines = []
    skip_next = False
    lines = code.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # rootComp.name = ... crashes (also comp.name / root.name / component.name)
        # documents.add(...) spawns a NEW Untitled doc every run -> hits Fusion's
        # document limit (Read-Only). Work on the provided active design instead.
        if _re.search(r'documents\s*\.\s*add\s*\(', stripped):
            safe_lines.append('# ' + line.lstrip() + '  # REMOVED: use the active design, do not create new documents')
            i += 1
            continue

        if _re.search(r'\b(rootComp|comp|root|component)\.name\s*=', stripped):
            safe_lines.append('# ' + line.lstrip() + '  # REMOVED: component name cannot be changed')
            i += 1
            continue

        # addNewComponent → use rootComp directly
        # Pattern: occ = rootComp.occurrences.addNewComponent(...)
        m_occ = _re.match(r'^(\s*)(\w+)\s*=\s*rootComp\.occurrences\.addNewComponent\(', line)
        if m_occ:
            indent = m_occ.group(1)
            occ_var = m_occ.group(2)
            safe_lines.append(f"{indent}# addNewComponent replaced — use rootComp directly")
            # Skip the next line if it's occ.component
            if i + 1 < len(lines):
                next_stripped = lines[i + 1].strip()
                m_comp = _re.match(r'^(\s*)(\w+)\s*=\s*' + occ_var + r'\.component', lines[i + 1])
                if m_comp:
                    comp_var = m_comp.group(2)
                    safe_lines.append(f"{m_comp.group(1)}{comp_var} = rootComp")
                    i += 2
                    continue
            i += 1
            continue

        # Fix invalid userParameters.add() names (with uniqueness counter)
        def _fix_param_name(m, _used=_used_param_names):
            quote = m.group(1)
            raw = m.group(2)
            fixed = _sanitize_param_name(raw)
            # Make unique if collision
            base = fixed
            counter = 1
            while fixed in _used:
                fixed = f'{base}_{counter}'
                counter += 1
            _used.add(fixed)
            return f".add({quote}{fixed}{quote},"
        line = _re.sub(r'\.add\(([\'"])(.*?)\1,', _fix_param_name, line)
        safe_lines.append(line)
        i += 1

    full_code = '\n'.join(safe_lines)

    # ── Auto-fix sketch curve path errors ──
    # Fix 1: sketch.sketchLines → sketch.sketchCurves.sketchLines
    #         (lookbehind ensures we don't double-insert .sketchCurves)
    full_code = _re.sub(r'(?<!sketchCurves)\.sketchLines\b', '.sketchCurves.sketchLines', full_code)
    full_code = _re.sub(r'(?<!sketchCurves)\.sketchCircles\b', '.sketchCurves.sketchCircles', full_code)
    full_code = _re.sub(r'(?<!sketchCurves)\.sketchArcs\b', '.sketchCurves.sketchArcs', full_code)

    # Fix 2: sketchCurves.addX() → sketchCurves.sketchLines/Circles/Arcs.addX()
    full_code = _re.sub(r'\.sketchCurves\.addByTwoPoints\b', '.sketchCurves.sketchLines.addByTwoPoints', full_code)
    full_code = _re.sub(r'\.sketchCurves\.addTwoPointRectangle\b', '.sketchCurves.sketchLines.addTwoPointRectangle', full_code)
    full_code = _re.sub(r'\.sketchCurves\.addByCenterRadius\b', '.sketchCurves.sketchCircles.addByCenterRadius', full_code)
    full_code = _re.sub(r'\.sketchCurves\.addByCenterStartEnd\b', '.sketchCurves.sketchArcs.addByCenterStartEnd', full_code)

    return full_code


def _get_model_context() -> dict:
    """Collect a JSON-serialisable snapshot of the active design. Must run on main thread."""
    try:
        design   = adsk.fusion.Design.cast(_app.activeProduct)
        rootComp = design.rootComponent

        bodies = []
        for i in range(rootComp.bRepBodies.count):
            b = rootComp.bRepBodies.item(i)
            try:
                pp = b.physicalProperties
                vol_cm3 = round(pp.volume, 4)
                mass_kg = round(pp.mass, 4)
            except Exception:
                vol_cm3 = mass_kg = None
            bodies.append({
                'name': b.name,
                'visible': b.isVisible,
                'volume_cm3': vol_cm3,
                'mass_kg': mass_kg,
            })

        params = {}
        up = design.userParameters
        for i in range(up.count):
            p = up.item(i)
            params[p.name] = {
                'value': round(p.value, 6),
                'unit': p.unit,
                'expression': p.expression,
            }

        features = []
        tl = design.timeline
        for i in range(min(tl.count, 50)):
            try:
                item = tl.item(i)
                ent = item.entity
                features.append({
                    'index': i,
                    'name': ent.name if ent else f'item_{i}',
                    'type': type(ent).__name__ if ent else 'unknown',
                    'suppressed': item.isSuppressed,
                })
            except Exception:
                features.append({'index': i, 'name': f'item_{i}', 'type': 'unknown'})

        # Overall bounding-box dimensions in MM — the key measurement for
        # verifying the built part against the requested dimensions.
        bbox_mm = None
        try:
            bb = rootComp.boundingBox
            if bb:
                bbox_mm = {
                    'x': round((bb.maxPoint.x - bb.minPoint.x) * 10, 2),
                    'y': round((bb.maxPoint.y - bb.minPoint.y) * 10, 2),
                    'z': round((bb.maxPoint.z - bb.minPoint.z) * 10, 2),
                }
        except Exception:
            bbox_mm = None

        return {
            'component_name': rootComp.name,
            'body_count': rootComp.bRepBodies.count,
            'bodies': bodies,
            'parameters': params,
            'features': features,
            'bbox_mm': bbox_mm,
        }
    except Exception as e:
        return {'error': str(e)}


def _clear_root(root):
    """Wipe the active design to a clean slate (bodies, sketches, occurrences,
    construction geometry). Run on the main thread BEFORE a fresh build/retry so
    attempts don't accumulate Body / Body(1) / Body(2) duplicates."""
    for name in ('bRepBodies', 'sketches', 'occurrences',
                 'constructionPlanes', 'constructionAxes', 'constructionPoints'):
        try:
            coll = getattr(root, name)
            while coll.count > 0:
                try:
                    coll.item(0).deleteMe()
                except Exception:
                    break
        except Exception:
            pass


def _execute_build(code, app_ref):
    code = _sanitize_build_code(code)
    ok, reason = _validate_code(code)
    if not ok:
        return False, reason, {}

    try:
        compiled = compile(code, '<cadassist>', 'exec')
    except SyntaxError as e:
        return False, f'Syntax error: {e}', {}

    design   = adsk.fusion.Design.cast(app_ref.activeProduct)
    root     = design.rootComponent
    timeline = design.timeline
    mark     = timeline.count

    ns = _make_namespace()
    import math
    ns['adsk']     = adsk
    ns['app']      = app_ref
    ns['ui']       = app_ref.userInterface
    ns['design']   = design
    ns['rootComp'] = root
    ns['math']     = math
    ns['__builtins__']['adsk'] = adsk

    try:
        exec(compiled, ns)
    except Exception as e:
        return False, f'Definition error: {e}', {}

    build_fn = ns.get('build')
    if not callable(build_fn):
        return False, 'No build() function found', {}

    t0 = time.time()
    try:
        result = build_fn(root, {})
        adsk.doEvents()
        app_ref.activeViewport.refresh()
    except Exception as e:
        try:
            if timeline.count > mark:
                timeline.item(mark).rollTo(True)
        except Exception:
            pass
        return False, f'Runtime: {e}\n{traceback.format_exc()}', {}

    elapsed = round((time.time() - t0) * 1000)
    info = result if isinstance(result, dict) else {}
    info['exec_time_ms'] = elapsed

    params = {}
    up = design.userParameters
    for i in range(up.count):
        p = up.item(i)
        params[p.name] = {'value': p.value, 'unit': p.unit, 'expression': p.expression}
    info['params'] = params

    return True, f'Built in {elapsed}ms', info


def _execute_drawing(code, app_ref, gdt_data):
    ok, reason = _validate_code(code)
    if not ok:
        return False, reason

    try:
        compiled = compile(code, '<cadassist_draw>', 'exec')
    except SyntaxError as e:
        return False, f'Syntax: {e}'

    ns = _make_namespace()
    import math
    ns['adsk'] = adsk
    ns['app']  = app_ref
    ns['math'] = math
    ns['__builtins__']['adsk'] = adsk

    try:
        exec(compiled, ns)
    except Exception as e:
        return False, f'Definition: {e}'

    fn = ns.get('create_drawing')
    if not callable(fn):
        return False, 'No create_drawing() found'

    # Pass the REAL component to draw (was None — the drawing prompt tells the
    # model to use `component` in addBaseView(component, ...), so None broke it).
    try:
        design = adsk.fusion.Design.cast(app_ref.activeProduct)
        component = design.rootComponent if design else None
    except Exception:
        component = None
    if component is None:
        return False, 'אין מודל פעיל לשרטוט'

    try:
        fn(app_ref, component, gdt_data or {})
        adsk.doEvents()
        return True, 'Drawing created'
    except Exception as e:
        return False, f'Drawing: {e}'


# ══════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════

def _do_export(fmt):
    """Export active design to STEP or STL. Returns (ok, path_or_msg)."""
    try:
        design  = adsk.fusion.Design.cast(_app.activeProduct)
        exp_mgr = design.exportManager
        out_dir = tempfile.gettempdir()

        if fmt == 'step':
            path = os.path.join(out_dir, 'CADAssistant_export.step')
            opts = exp_mgr.createSTEPExportOptions(path)
            ok   = exp_mgr.execute(opts)
        else:  # stl
            path = os.path.join(out_dir, 'CADAssistant_export.stl')
            opts = exp_mgr.createSTLExportOptions(design.rootComponent, path)
            opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
            ok   = exp_mgr.execute(opts)

        if ok:
            # Open folder on Windows (use os.startfile — safer than subprocess)
            try:
                os.startfile(os.path.dirname(path))
            except Exception:
                pass
            return True, path
        return False, 'Export failed'
    except Exception as e:
        return False, str(e)


def _do_generate_bom():
    """Generate BOM from user parameters."""
    try:
        design = adsk.fusion.Design.cast(_app.activeProduct)
        up = design.userParameters
        if up.count == 0:
            return 'No user parameters found in the active design.'

        lines = ['Parameter Name          Value        Unit']
        lines.append('─' * 45)
        for i in range(up.count):
            p = up.item(i)
            val_str = f'{p.value:.4f}'.rstrip('0').rstrip('.')
            lines.append(f'{p.name:<24}{val_str:<13}{p.unit}')

        lines.append('─' * 45)
        lines.append(f'Total parameters: {up.count}')
        return '\n'.join(lines)
    except Exception as e:
        return f'BOM error: {e}'


# ══════════════════════════════════════════════════════════════
# MESSAGING HELPERS
# ══════════════════════════════════════════════════════════════

def _send(msg_type, text):
    if _palette:
        try:
            _palette.sendInfoToHTML('message', json.dumps({'type': msg_type, 'text': text}))
        except Exception:
            pass


def _send_progress(stage, total, label=''):
    if _palette:
        try:
            _palette.sendInfoToHTML('progress', json.dumps(
                {'stage': stage, 'total': total, 'label': label}
            ))
        except Exception:
            pass


def _send_validation(report):
    if _palette:
        try:
            errors   = [{'id': e.rule_id, 'msg': e.message, 'fix': e.suggestion or ''} for e in report.errors]
            warnings = [{'id': w.rule_id, 'msg': w.message} for w in report.warnings]
            infos    = [{'msg': i.message} for i in report.infos]
            _palette.sendInfoToHTML('validation', json.dumps(
                {'score': report.score, 'errors': errors, 'warnings': warnings, 'infos': infos}
            ))
        except Exception:
            pass


def _send_params(params):
    if _palette and params:
        try:
            rows = [{'name': k, 'val': f'{v["value"]:.4f}'.rstrip('0').rstrip('.'),
                     'unit': v['unit'], 'expr': v['expression']}
                    for k, v in params.items()]
            _palette.sendInfoToHTML('params', json.dumps({'rows': rows}))
        except Exception:
            pass


def _send_analysis(text):
    """Send image analysis text to panel."""
    if _palette:
        try:
            _palette.sendInfoToHTML('analysis', json.dumps({'text': text}))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# CUSTOM EVENT HANDLER (main-thread execution)
# ══════════════════════════════════════════════════════════════

class ExecuteCustomEvent(adsk.core.CustomEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        global _exec_result, _last_timeline_mark
        try:
            payload = json.loads(args.additionalInfo)
            mode    = payload.get('mode', 'model')

            if mode == 'model':
                design = adsk.fusion.Design.cast(_app.activeProduct)
                if payload.get('clear_first'):
                    _clear_root(design.rootComponent)   # clean slate (esp. on retry)
                _last_timeline_mark = design.timeline.count
                ok, msg, info = _execute_build(payload['code'], _app)
                _exec_result = {'ok': ok, 'msg': msg, 'info': info}

            elif mode == 'delete_last':
                # Guard: mark<=0 means "no recorded build". Without this, the
                # range below becomes range(end-1, -1, -1) and wipes the WHOLE
                # timeline instead of just the last build.
                if _last_timeline_mark <= 0:
                    # Not an error — just nothing this session recorded to undo.
                    _exec_result = {'ok': False, 'noop': True, 'msg': 'אין פעולה אחרונה למחיקה'}
                else:
                    design = adsk.fusion.Design.cast(_app.activeProduct)
                    tl = design.timeline
                    end = tl.count
                    deleted = 0
                    for i in range(end - 1, _last_timeline_mark - 1, -1):
                        try:
                            tl.item(i).entity.deleteMe()
                            deleted += 1
                        except Exception:
                            pass
                    _last_timeline_mark = 0
                    _exec_result = {'ok': True, 'msg': f'מחק {deleted} פעולות'}

            elif mode == 'clear_since':
                # Internal: delete timeline items from an EXPLICIT mark to the end.
                # Used by the verification loop to remove the original build before
                # rebuilding a corrected one. No mark<=0 guard (unlike delete_last)
                # because the caller passes the exact mark it captured.
                mark = int(payload.get('mark', 0))
                design = adsk.fusion.Design.cast(_app.activeProduct)
                tl = design.timeline
                for i in range(tl.count - 1, mark - 1, -1):
                    try:
                        tl.item(i).entity.deleteMe()
                    except Exception:
                        pass
                _exec_result = {'ok': True}

            elif mode == 'clear_model':
                # Delete EVERYTHING in the active design. Unlike delete_last this
                # is session-independent — it removes a model built any time,
                # including before a reload. Undoable in Fusion via Ctrl+Z.
                design = adsk.fusion.Design.cast(_app.activeProduct)
                if not design:
                    _exec_result = {'ok': False, 'msg': 'אין מודל פעיל'}
                else:
                    removed = 0
                    # Parametric designs: wiping the timeline removes all
                    # features, sketches and the bodies they produced.
                    try:
                        tl = design.timeline
                        for i in range(tl.count - 1, -1, -1):
                            try:
                                tl.item(i).entity.deleteMe()
                                removed += 1
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # Direct-modeling fallback: delete any bodies left over.
                    try:
                        bodies = design.rootComponent.bRepBodies
                        for i in range(bodies.count - 1, -1, -1):
                            try:
                                bodies.item(i).deleteMe()
                                removed += 1
                            except Exception:
                                pass
                    except Exception:
                        pass
                    _last_timeline_mark = 0
                    if removed > 0:
                        _exec_result = {'ok': True, 'msg': f'נוקו {removed} פריטים מהמודל'}
                    else:
                        _exec_result = {'ok': False, 'noop': True, 'msg': 'המודל כבר ריק'}

            elif mode == 'get_context':
                ctx = _get_model_context()
                _exec_result = {'ok': True, 'context': ctx}

            elif mode == 'run_query':
                code = payload.get('code', '')
                try:
                    compiled = compile(code, '<query>', 'exec')
                    ns = {'adsk': adsk, '__builtins__': __builtins__}
                    exec(compiled, ns)
                    fn = ns.get('query')
                    if callable(fn):
                        design2 = adsk.fusion.Design.cast(_app.activeProduct)
                        rootComp2 = design2.rootComponent
                        answer = fn(design2, rootComp2)
                        _exec_result = {'ok': True, 'answer': str(answer)}
                    else:
                        _exec_result = {'ok': False, 'msg': 'No query() function found'}
                except Exception as e:
                    _exec_result = {'ok': False, 'msg': str(e)}

            elif mode == 'run_edit':
                code = payload.get('code', '')
                tl_mark = payload.get('timeline_mark', 0)
                design3 = adsk.fusion.Design.cast(_app.activeProduct)
                rootComp3 = design3.rootComponent
                tl3 = design3.timeline
                pre_mark = tl3.count
                try:
                    compiled = compile(code, '<edit>', 'exec')
                    ns = {'adsk': adsk, 'design': design3, '__builtins__': __builtins__}
                    exec(compiled, ns)
                    fn = ns.get('modify')
                    if callable(fn):
                        result = fn(design3, rootComp3, tl_mark)
                        adsk.doEvents()
                        _app.activeViewport.refresh()
                        _exec_result = {'ok': True, 'info': result or {}}
                    else:
                        _exec_result = {'ok': False, 'msg': 'No modify() function found'}
                except Exception as e:
                    try:
                        for i in range(tl3.count - 1, pre_mark - 1, -1):
                            try:
                                tl3.item(i).entity.deleteMe()
                            except Exception:
                                pass
                    except Exception:
                        pass
                    _exec_result = {'ok': False, 'msg': f'Edit failed: {e}', 'rolled_back': True}

            elif mode == 'manage_params':
                action_type = payload.get('action_type', 'list')
                design4 = adsk.fusion.Design.cast(_app.activeProduct)
                up4 = design4.userParameters
                if action_type == 'list':
                    rows = []
                    for i in range(up4.count):
                        p = up4.item(i)
                        rows.append({
                            'name': p.name,
                            'value': round(p.value, 6),
                            'unit': p.unit,
                            'expression': p.expression,
                        })
                    _exec_result = {'ok': True, 'params': rows}
                elif action_type == 'set':
                    pname = payload.get('name', '')
                    expr  = payload.get('expression', '0')
                    unit  = payload.get('unit', 'cm')
                    comment = payload.get('comment', '')
                    try:
                        existing = up4.itemByName(pname)
                        if existing:
                            existing.expression = expr
                            _exec_result = {'ok': True, 'msg': f'עדכן {pname} = {expr} {unit}'}
                        else:
                            up4.add(pname, adsk.core.ValueInput.createByString(expr), unit, comment)
                            _exec_result = {'ok': True, 'msg': f'נוצר {pname} = {expr} {unit}'}
                    except Exception as e:
                        _exec_result = {'ok': False, 'msg': str(e)}

            elif mode == 'drawing':
                ok, msg = _execute_drawing(payload['code'], _app, payload.get('gdt_data'))
                _exec_result = {'ok': ok, 'msg': msg}

            elif mode == 'assembly_real':
                # REAL assembly: build_assembly creates its own Components + Joints.
                # Run WITHOUT the addNewComponent-stripping sanitizer, then VALIDATE
                # (>=2 components, no bodies under root, a non-rigid joint if motion
                # is required) and report Success only if it genuinely passes.
                code = payload.get('code', '')
                need_motion = bool(payload.get('need_motion'))
                ok_v, reason = _validate_code(code)
                design = adsk.fusion.Design.cast(_app.activeProduct)
                if not ok_v:
                    _exec_result = {'ok': False, 'msg': reason}
                elif not design:
                    _exec_result = {'ok': False, 'msg': 'אין עיצוב פעיל'}
                else:
                    rootComp = design.rootComponent
                    _clear_root(rootComp)   # assemblies always start from a clean slate
                    ns = _make_namespace()
                    ns['adsk'] = adsk; ns['app'] = _app; ns['ui'] = _app.userInterface
                    ns['design'] = design; ns['rootComp'] = rootComp
                    ns['__builtins__']['adsk'] = adsk
                    try:
                        exec(compile(code, '<assembly_real>', 'exec'), ns)
                        fn = ns.get('build_assembly')
                        if not callable(fn):
                            _exec_result = {'ok': False, 'msg': 'No build_assembly() found'}
                        else:
                            info = fn(rootComp)
                            adsk.doEvents()
                            n_occ = rootComp.occurrences.count
                            n_joints, nonrigid = 0, 0
                            for coll in (rootComp.joints, rootComp.asBuiltJoints):
                                for i in range(coll.count):
                                    n_joints += 1
                                    try:
                                        jt = coll.item(i).jointMotion.jointType
                                        if jt != adsk.fusion.JointTypes.RigidJointType:
                                            nonrigid += 1
                                    except Exception:
                                        pass
                            root_bodies = rootComp.bRepBodies.count
                            problems = []
                            if n_occ < 2:
                                problems.append('רק {} components (צריך >=2)'.format(n_occ))
                            if root_bodies > 0:
                                problems.append('{} bodies תחת Root (אסור בהרכבה)'.format(root_bodies))
                            if need_motion and nonrigid == 0:
                                problems.append('אין joint לא-קשיח — אין תנועה')
                            result = {'components': n_occ, 'joints': n_joints,
                                      'nonrigid': nonrigid, 'root_bodies': root_bodies,
                                      'info': info if isinstance(info, dict) else {}}
                            if problems:
                                result['ok'] = False
                                result['msg'] = 'Validation נכשל: ' + '; '.join(problems)
                            else:
                                result['ok'] = True
                            _exec_result = result
                    except Exception as e:
                        emsg = '{}'.format(e)
                        if 'one component' in emsg or 'Part Design' in emsg:
                            emsg += '  ← המסמך במצב Part (component יחיד). פתח Design חדש (File → New Design) ונסה שוב.'
                        _exec_result = {'ok': False, 'msg': emsg}

            elif mode == 'export':
                ok, msg = _do_export(payload.get('fmt', 'step'))
                _exec_result = {'ok': ok, 'msg': msg}

            elif mode == 'bom':
                bom_text = _do_generate_bom()
                _exec_result = {'ok': True, 'msg': bom_text}

            elif mode == 'screenshot':
                try:
                    import base64
                    path = os.path.join(tempfile.gettempdir(), 'cadassist_shot.png')
                    vp = _app.activeViewport
                    vp.fit()
                    adsk.doEvents()
                    ok = vp.saveAsImageFile(path, 440, 300)
                    if ok and os.path.exists(path):
                        with open(path, 'rb') as f:
                            b64 = base64.b64encode(f.read()).decode('utf-8')
                        _exec_result = {'ok': True, 'b64': b64}
                    else:
                        _exec_result = {'ok': False, 'msg': 'Screenshot failed'}
                except Exception as e:
                    _exec_result = {'ok': False, 'msg': str(e)}

        except Exception:
            _exec_result = {'ok': False, 'msg': traceback.format_exc()}
        finally:
            _exec_done.set()


def _fire_and_wait(payload_dict, timeout=90):
    """Fire custom event, block thread until main thread finishes.

    Serialized by _exec_lock so two concurrent background threads can't both
    reset/read the shared _exec_result / _exec_done globals and corrupt each
    other's results. The single main thread runs the work, so serializing the
    callers here is correct and cheap.
    """
    global _exec_result
    with _exec_lock:
        _exec_result = {}
        _exec_done.clear()
        _app.fireCustomEvent(EXECUTE_EVENT_ID, json.dumps(payload_dict))
        finished = _exec_done.wait(timeout=timeout)
        if not finished:
            return {'ok': False, 'msg': f'הפעולה לא הסתיימה תוך {timeout} שניות (timeout)'}
        return _exec_result


# ══════════════════════════════════════════════════════════════
# DETECT INTENT
# ══════════════════════════════════════════════════════════════

def _is_edit(text):
    low = text.lower()
    return any(k in low for k in EDIT_KEYWORDS_HE + EDIT_KEYWORDS_EN)


def _is_reset(text):
    low = text.lower()
    return any(k in low for k in RESET_KEYWORDS)


# Words that imply a MOVING mechanism → build with real components + joints.
MOTION_KEYWORDS = [
    'assembly', 'joint', 'hinge', 'motion', 'rotate', 'rotating', 'move', 'moving',
    'slider', 'revolute', 'mechanism', 'gear', 'robot', 'robotic', 'linkage', 'pivot',
    'ציר', 'מנגנון', 'תנועה', 'מסתובב', 'סיבוב', 'גלגל שיניים', 'זרוע', 'מפרק', 'נע',
]


def _is_motion_assembly(text):
    low = (text or '').lower()
    return any(k in low for k in MOTION_KEYWORDS)


def _lang(text):
    """Detect language: 'he' or 'en'."""
    he_chars = sum(1 for c in text if '\u05d0' <= c <= '\u05ea')
    return 'he' if he_chars > 2 else 'en'


# ══════════════════════════════════════════════════════════════
# DESIGN PIPELINE THREAD
# ══════════════════════════════════════════════════════════════

def _interpret_and_confirm(prompt):
    """Ask AI to interpret the request, show plan to user, wait for confirm/cancel.
    Returns True if user confirmed, False if cancelled or timed-out."""
    global _confirm_event, _confirm_result
    try:
        plan = _simple_call(INTERPRET_SYSTEM_PROMPT, prompt, max_tokens=512)
    except Exception as e:
        _send('error', f'Interpret error: {e}')
        return False
    # Send plan to HTML — panel shows confirm/cancel buttons
    if _palette:
        import json as _json
        _palette.sendInfoToHTML('show_plan', _json.dumps({'plan': plan}))
    # Wait for user response (5 minutes timeout)
    _confirm_event.clear()
    _confirm_result = False
    confirmed = _confirm_event.wait(timeout=300)
    if not confirmed or not _confirm_result:
        _send('system', 'ביטול — הפעולה לא בוצעה.')
        _send_progress(0, 0)
        return False
    return True


def _maybe_expand(prompt, params):
    """If 'auto_detail' is on, expand a short request into a full engineering
    spec (more detail → better, more accurate models) and show it. Returns the
    expanded prompt, or the original on failure / when disabled / on Ollama."""
    if not params.get('auto_detail') or _provider == 'ollama':
        return prompt
    _send('system', '📝 מפרט את הבקשה לספסיפיקציה הנדסית מלאה...')
    try:
        spec = _call_ai(EXPAND_SYSTEM_PROMPT, [{'role': 'user', 'content': prompt}], max_tokens=1024)
        spec = (spec or '').strip()
        if len(spec) > len(prompt):
            _send('code', spec)   # show the expanded spec to the user
            return spec
    except Exception as e:
        _send('system', f'פירוט אוטומטי דולג: {e}')
    return prompt


def _engineering_report(material, process):
    """Print a concise engineering summary of the built part from the live model."""
    try:
        ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
        ctx = ctx_res.get('context', {}) if ctx_res.get('ok') else {}
        if not ctx or 'error' in ctx:
            return
        bbox   = ctx.get('bbox_mm') or {}
        bodies = ctx.get('bodies', [])
        total_mass = sum((b.get('mass_kg') or 0) for b in bodies)
        mat = MATERIAL_PROPS.get(material, {})

        L = ['PART REPORT', '=' * 28]
        L.append('Name:      {}'.format(ctx.get('component_name', '?')))
        L.append('Material:  {}  (yield {} MPa)'.format(material, mat.get('yield', '?')))
        L.append('Process:   {}'.format(process))
        if bbox:
            L.append('Size:      {} x {} x {} mm'.format(bbox.get('x', '?'), bbox.get('y', '?'), bbox.get('z', '?')))
        L.append('Bodies: {}   Features: {}   Params: {}'.format(
            ctx.get('body_count', '?'), len(ctx.get('features', [])), len(ctx.get('parameters', {}))))
        L.append('Est. mass: {} kg'.format(round(total_mass, 4)))

        warns = []
        for b in bodies:
            if re.match(r'^(Body|Component|Sketch)\d+$', b.get('name', '')):
                warns.append('generic name "{}"'.format(b.get('name')))
        if not ctx.get('parameters'):
            warns.append('no user parameters (not parametric)')
        L.append('Warnings:  ' + ('; '.join(warns[:4]) if warns else 'none'))
        _send('code', '\n'.join(L))
    except Exception:
        pass


def _verify_and_correct(request, code):
    """Measure the just-built part and, if a requested dimension drifted beyond
    ~0.5 mm, delete it and rebuild a corrected version ONCE. Returns the final
    code (corrected or original). Updates _last_code / _last_params on rebuild."""
    global _last_code, _last_params
    # Capture the original build's timeline mark BEFORE anything else, so we can
    # remove exactly that build if we need to rebuild a correction.
    orig_mark = _last_timeline_mark
    try:
        ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
        ctx = ctx_res.get('context', {}) if ctx_res.get('ok') else {}
        bbox = ctx.get('bbox_mm')
        params = ctx.get('parameters', {})
        if not bbox and not params:
            return code  # nothing measurable to verify against

        measured = {
            'overall_bounding_box_mm': bbox,
            'parameters': {k: f"{v['value']} {v.get('unit', '')}".strip()
                           for k, v in params.items()},
        }
        _send('system', '🔍 מאמת מידות מול הבקשה...')

        verify_msg = f"""You are a CAD QC inspector. The user requested this part:
"{request}"

The model that was actually built measures:
{json.dumps(measured, ensure_ascii=False, indent=2)}

Compare the ACTUAL measurements against the dimensions the user requested.
(bounding_box = overall envelope in mm; parameters = internal feature sizes.)
- If every requested dimension is satisfied within 0.5 mm, reply with EXACTLY
  this token and nothing else: VERIFIED_OK
- Otherwise return a corrected def build(rootComp, config) that fixes ONLY the
  out-of-spec dimensions, keeping everything else identical, with a one-line
  comment naming what changed."""

        vresp = _call_ai(MODELING_SYSTEM_PROMPT, [{'role': 'user', 'content': verify_msg}])

        fixed = _extract_python(vresp, 'def build(') if 'def build(' in vresp else None
        if not fixed:
            _send('success', '✓ מאומת — המידות תואמות לבקשה')
            return code

        # Discrepancy found → wipe EVERYTHING and rebuild the fix clean
        # (clear_first guarantees no duplicate Body / Body(1) leftovers).
        _send('system', '⚙️ נמצאה סטייה — מתקן ובונה מחדש...')
        rb = _fire_and_wait({'mode': 'model', 'code': fixed, 'clear_first': True})
        if rb.get('ok'):
            _last_code = fixed
            info = rb.get('info', {})
            _last_params = info.get('params', _last_params)
            _send('success', '✓ תוקן ואומת — המידות עכשיו תואמות לבקשה')
            if info.get('params'):
                _send_params(info['params'])
            return fixed
        # Correction failed — rebuild the original clean so the doc isn't empty.
        _send('system', f'התיקון נכשל — משחזר את המודל המקורי')
        _fire_and_wait({'mode': 'model', 'code': code, 'clear_first': True})
        return code
    except Exception as e:
        _send('system', f'אימות דולג: {e}')
        return code


def _pipeline_thread(params):
    global _conversation, _last_code, _last_params, _last_comp_name
    try:
        _pipeline_thread_inner(params)
    except Exception:
        _send('error', f'Pipeline crash:\n{traceback.format_exc()}')
    finally:
        # ALWAYS clear the busy state, regardless of which path the pipeline
        # took. Some early returns only send a 'system' message, which does NOT
        # reset the UI — without this the Send button stays disabled forever.
        _send_progress(0, 0)


def _pipeline_thread_inner(params):
    global _conversation, _last_code, _last_params, _last_comp_name

    prompt      = params.get('text', params.get('prompt', ''))
    material    = params.get('material', 'Steel')
    process     = params.get('process', 'CNC Machining')
    detail      = params.get('detail', 'standard')
    units       = params.get('units', 'mm')
    output_mode = params.get('output_mode', 'model')
    show_code   = params.get('show_code', False)
    compare     = params.get('compare', False)
    use_api    = (_provider != 'ollama')   # True for claude / groq / gemini

    do_gdt      = use_api and output_mode in ('model_gdt', 'full')
    do_drawing  = False   # Fusion API cannot create drawings programmatically (platform limitation)
    do_validate = use_api and output_mode in ('model_gdt', 'full')
    total       = 1 + do_gdt + do_drawing + do_validate + (1 if compare else 0)
    stage       = 0

    # Reset conversation if requested
    if _is_reset(prompt):
        _conversation = []
        _last_code    = ''
        _last_params  = {}
        _send('system', 'Context cleared — starting fresh.')

    # Auto-detail: expand a short request into a full engineering spec first.
    prompt = _maybe_expand(prompt, params)

    full_prompt = f"""{prompt}

Material: {material}
Manufacturing process: {process}
Units: {units}"""

    # Build message list for Claude (multi-turn for edits)
    editing = _is_edit(prompt) and _last_code and not _is_reset(prompt)
    if editing and use_api:
        edit_ctx = f"""EXISTING PART (modify this, do not recreate from scratch):
Previous parameters: {json.dumps({k: v['expression'] for k, v in _last_params.items()}, indent=2)[:1200]}

Previous code (abbreviated):
```python
{_last_code[:2000]}
```

USER MODIFICATION REQUEST: {full_prompt}
Return the complete updated def build() incorporating the changes."""
        user_content = edit_ctx
        _send('system', 'Smart edit mode — modifying existing part...')
    else:
        user_content = f"""Create a Fusion 360 parametric model:

DESCRIPTION: {full_prompt}

SETTINGS:
- Detail level: {detail}
- Units: {units}
- Create as new component
- Include user parameters for ALL dimensions
- Use isComputeDeferred for performance
- Name all bodies and features

Return ONLY the `def build(rootComp, config):` function."""

    # Add to conversation history
    _conversation.append({'role': 'user', 'content': user_content})
    # Keep only last 6 messages (3 pairs)
    if len(_conversation) > 6:
        _conversation = _conversation[-6:]

    # ── Stage 1: 3D Model ──
    stage += 1
    _send_progress(stage, total, 'Generating 3D model...')
    _send('system', f'[{stage}/{total}] AI generating model code...')

    try:
        if use_api:
            sys_prompt = GROQ_MODELING_PROMPT if _provider == 'groq' else MODELING_SYSTEM_PROMPT
            response = _call_ai(sys_prompt, _conversation)
        else:
            response = _call_ollama(full_prompt)
    except Exception as e:
        _send('error', f'AI error: {e}')
        _send_progress(0, 0)
        return

    code = _extract_python(response, 'def build(')
    if not code and 'def build(' in response:
        code = response.strip()

    if not code or 'def build(' not in code:
        _send('error', 'AI did not return a valid build() function. Try rephrasing.')
        _send_progress(0, 0)
        return

    if show_code:
        _send('code', code[:2000] + ('...' if len(code) > 2000 else ''))

    # Add assistant response to conversation
    if use_api:
        _conversation.append({'role': 'assistant', 'content': response})

    # Pre-execution lint — catch obvious issues before Fusion even tries.
    # Lint the SANITIZED code (the same transform execution applies) so issues
    # the sanitizer already auto-fixes don't waste a full AI fix-call.
    lint_issues = _lint_build_code(_sanitize_build_code(code))
    if lint_issues:
        _send('system', f'בעיות נמצאו לפני ביצוע — מתקן אוטומטית...')
        fix_prompt = f"""This Fusion 360 build() has problems:
{chr(10).join(f'- {i}' for i in lint_issues)}

Code:
```python
{code[:2000]}
```

Fix ALL issues and return the corrected def build()."""
        try:
            if use_api:
                fix_resp = _call_ai(MODELING_SYSTEM_PROMPT,
                                    [{'role': 'user', 'content': fix_prompt}])
            else:
                fix_resp = _call_ollama(fix_prompt)
            fixed = _extract_python(fix_resp, 'def build(')
            if fixed:
                code = fixed
                if show_code:
                    _send('code', code[:2000])
        except Exception:
            pass

    _send('system', f'[{stage}/{total}] Building in Fusion 360...')
    res = _fire_and_wait({'mode': 'model', 'code': code})

    if not res.get('ok'):
        # Auto-retry up to 3 times with targeted error context
        current_code = code
        for attempt in range(3):
            _send('system', f'Build failed — retrying ({attempt+1}/3)...')
            err_msg = res.get('msg', 'unknown error')

            # Extract broken line for syntax errors
            broken_line = ''
            import re as _re2
            m = _re2.search(r'line (\d+)', err_msg)
            if m and 'Syntax' in err_msg:
                lineno = int(m.group(1))
                lines = current_code.splitlines()
                if 0 < lineno <= len(lines):
                    broken_line = f'\nBroken line {lineno}: {lines[lineno-1].strip()}'

            retry_msg = f"""FAILED with error:
{err_msg}{broken_line}

Broken code:
```python
{current_code[:2000]}
```

Fix ONLY what caused the error. KEEP every requested feature (holes, keyways,
threads, patterns, fillets) — do NOT simplify the part to avoid the error.
Correct-API reminders:
- isComputeDeferred=False BEFORE reading .profiles
- ALL dims in CM (mm÷10)
- A ring/annulus = 2 concentric circles in ONE sketch (profile.union() doesn't exist)
- Polygons via sk.sketchCurves.sketchLines.addByTwoPoints (no sketchPolygon)
- Parameter names: ASCII letters/digits/underscore only
- Real threads: comp.features.threadFeatures.createInput(face, True) (NOT ThreadFeatureInputParameters)
- config is always {{}} — define all dims as local variables, never config['...']

Rewrite the COMPLETE def build() function, keeping the original design intent."""

            try:
                if use_api:
                    retry_conv = [{'role': 'user', 'content': user_content},
                                  {'role': 'assistant', 'content': response},
                                  {'role': 'user', 'content': retry_msg}]
                    retry_resp = _call_ai(MODELING_SYSTEM_PROMPT, retry_conv)
                else:
                    retry_resp = _call_ollama(retry_msg)
                retry_code = _extract_python(retry_resp, 'def build(')
                if retry_code:
                    if show_code:
                        _send('code', retry_code[:1500])
                    res = _fire_and_wait({'mode': 'model', 'code': retry_code, 'clear_first': True})
                    if res.get('ok'):
                        code = retry_code
                        break
                    current_code = retry_code
                else:
                    break
            except Exception as e:
                _send('system', f'Retry error: {e}')
                break

    if not res.get('ok'):
        _send('error', f'Model failed: {res.get("msg", "unknown")}')
        _send_progress(0, 0)
        return

    info = res.get('info', {})
    _last_code     = code
    _last_params   = info.get('params', {})

    _send('success', f'Model built in {info.get("exec_time_ms", "?")}ms')
    if info.get('params'):
        _send_params(info['params'])

    # ── Verification: measure the part and auto-correct dimension drift ──
    if use_api:
        code = _verify_and_correct(prompt, code)

    # ── Engineering report (name, material, dims, features, mass, warnings) ──
    _engineering_report(material, process)

    # Screenshot after successful build
    shot = _fire_and_wait({'mode': 'screenshot'}, timeout=10)
    if shot.get('ok') and shot.get('b64') and _palette:
        _palette.sendInfoToHTML('screenshot', json.dumps({'b64': shot['b64']}))

    # ── Comparison: Variant B ──
    if compare and use_api:
        stage += 1
        _send_progress(stage, total, 'Generating Variant B...')
        _send('system', f'[{stage}/{total}] Generating lightweight variant...')
        try:
            alt_msg = f"""Create a WEIGHT-OPTIMIZED variant of this part (Variant B):
{full_prompt}
Name the component "Variant_B". Place it offset by 150mm on the X axis from origin.
Reduce weight vs. the standard design by adding pockets, ribs, and thinner walls where safe."""
            alt_resp = _call_claude(MODELING_SYSTEM_PROMPT, [{'role': 'user', 'content': alt_msg}])
            alt_code = _extract_python(alt_resp, 'def build(')
            if alt_code:
                if show_code:
                    _send('code', alt_code[:1000])
                alt_res = _fire_and_wait({'mode': 'model', 'code': alt_code})
                if alt_res.get('ok'):
                    _send('success', f'Variant B built in {alt_res["info"].get("exec_time_ms", "?")}ms')
                else:
                    _send('system', f'Variant B failed: {alt_res.get("msg", "")}')
        except Exception as e:
            _send('system', f'Variant B error: {e}')

    # ── Stage 2: GD&T ──
    gdt_data = None
    if do_gdt:
        stage += 1
        _send_progress(stage, total, 'Generating GD&T...')
        _send('system', f'[{stage}/{total}] Analyzing geometry for GD&T...')
        try:
            gdt_msg = f"""Determine GD&T callouts for:
DESCRIPTION: {full_prompt}
PARAMETERS: {json.dumps({k: v['expression'] for k, v in _last_params.items()}, indent=2)[:800]}

Return ONLY a JSON specification with: datums, feature_controls, surface_finishes,
dimensional_tolerances, general_tolerance."""
            gdt_resp = _simple_call(GDT_SYSTEM_PROMPT, gdt_msg)
            gdt_data = _extract_json(gdt_resp)
            if gdt_data:
                nd = len(gdt_data.get('datums', []))
                nc = len(gdt_data.get('feature_controls', []))
                _send('success', f'GD&T: {nd} datums, {nc} feature controls')
            else:
                _send('system', 'GD&T: Could not parse response (continuing)')
        except Exception as e:
            _send('system', f'GD&T warning: {e}')

    # ── Stage 3: Drawing ──
    if do_drawing:
        stage += 1
        _send_progress(stage, total, 'Generating drawing...')
        _send('system', f'[{stage}/{total}] Creating mechanical drawing...')
        try:
            gdt_ctx = f'\nGD&T:\n{json.dumps(gdt_data, indent=2)}\n' if gdt_data else ''
            draw_msg = f"""Create a mechanical drawing for:
DESCRIPTION: {full_prompt}
{gdt_ctx}
Include standard views, section views, dimensions, GD&T, surface finish, title block.
Return ONLY `def create_drawing(app, component, gdt_data):`"""
            draw_resp = _simple_call(DRAWING_SYSTEM_PROMPT, draw_msg, max_tokens=8192)
            draw_code = _extract_python(draw_resp, 'def create_drawing(')
            if draw_code:
                if show_code:
                    _send('code', draw_code[:1200])
                draw_res = _fire_and_wait({'mode': 'drawing', 'code': draw_code, 'gdt_data': gdt_data or {}}, timeout=30)
                if draw_res.get('ok'):
                    _send('success', 'Drawing created')
                else:
                    _send('system', f'Drawing: {draw_res.get("msg", "")}')
            else:
                _send('system', 'Drawing: no valid code returned')
        except Exception as e:
            _send('system', f'Drawing warning: {e}')

    # ── Stage 4: Validation ──
    if do_validate:
        stage += 1
        _send_progress(stage, total, 'Validating design...')
        _send('system', f'[{stage}/{total}] Running validation checks...')
        try:
            from validation_engine import MasterValidator
            validator = MasterValidator()
            report    = validator.validate_all(
                component=None,
                process=process,
                material=material,
                params=_last_params,
                gdt_spec=gdt_data,
                has_drawing=do_drawing,
            )
            _send_validation(report)
            _send('system', f'Score: {report.score}/100 — {len(report.errors)} errors, {len(report.warnings)} warnings')
        except Exception as e:
            _send('system', f'Validation skipped: {e}')

    _send_progress(0, 0)
    _send('done', 'Complete! Use Export buttons to save STEP or STL.')


# ══════════════════════════════════════════════════════════════
# IMAGE REPAIR PIPELINE
# ══════════════════════════════════════════════════════════════

def _image_pipeline_thread(params):
    global _last_code, _last_params

    image_b64  = params.get('image_b64', '')
    media_type = params.get('media_type', 'image/jpeg')
    description = params.get('description', '')
    print_mat  = params.get('print_material', 'PLA')
    show_code  = params.get('show_code', False)

    if not image_b64:
        _send('error', 'No image data received.')
        return

    _send_progress(1, 3, 'Analyzing image...')
    _send('system', '[1/3] Claude analyzing the broken part...')

    user_text = f"""Please analyze this broken/damaged part and design a 3D-printable replacement.

Print material: {print_mat}
{f'Additional context: {description}' if description else ''}

Follow the response format exactly as specified in your instructions."""

    try:
        response = _call_claude_vision(image_b64, media_type, user_text, IMAGE_REPAIR_PROMPT)
    except Exception as e:
        _send('error', f'Vision API error: {e}')
        _send_progress(0, 0)
        return

    # Show analysis (everything before the code block)
    analysis_text = re.sub(r'```python.*?```', '', response, flags=re.DOTALL).strip()
    if analysis_text:
        _send_analysis(analysis_text)

    code = _extract_python(response, 'def build(')
    if not code or 'def build(' not in code:
        _send('error', 'Could not extract build() from AI response. Try a clearer image.')
        _send_progress(0, 0)
        return

    if show_code:
        _send('code', code[:2000] + ('...' if len(code) > 2000 else ''))

    _send_progress(2, 3, 'Building replacement part...')
    _send('system', '[2/3] Building replacement part in Fusion 360...')

    res = _fire_and_wait({'mode': 'model', 'code': code})

    if not res.get('ok'):
        # Retry
        _send('system', 'Build failed — retrying...')
        retry_msg = f"""Error: {res.get('msg', '')}
Fix the build() function. Ensure all dims in CM, isComputeDeferred used correctly."""
        try:
            retry_resp = _call_claude(MODELING_SYSTEM_PROMPT,
                                      [{'role': 'user', 'content': user_text},
                                       {'role': 'assistant', 'content': response},
                                       {'role': 'user', 'content': retry_msg}])
            retry_code = _extract_python(retry_resp, 'def build(')
            if retry_code:
                res = _fire_and_wait({'mode': 'model', 'code': retry_code, 'clear_first': True})
                if res.get('ok'):
                    code = retry_code
        except Exception:
            pass

    if not res.get('ok'):
        _send('error', f'Build failed: {res.get("msg", "")}')
        _send_progress(0, 0)
        return

    info       = res.get('info', {})
    _last_code = code
    _last_params = info.get('params', {})

    _send_progress(3, 3, 'Done!')
    _send('success', f'Replacement part built in {info.get("exec_time_ms", "?")}ms')
    if info.get('params'):
        _send_params(info['params'])

    _send_progress(0, 0)
    _send('done', 'Part ready for printing! Export as STL below.')


# ══════════════════════════════════════════════════════════════
# WEIGHT OPTIMIZATION
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# API KEY TEST + OLLAMA MODEL DETECTION
# ══════════════════════════════════════════════════════════════

def _test_api_key_thread(params):
    """Quick Claude API ping to validate the key."""
    key = params.get('key', _claude_api_key).strip()
    if not key:
        _send('error', 'אין API key — הכנס מפתח תחילה.')
        return
    _send('system', 'בודק API key...')
    try:
        payload = json.dumps({
            'model': CLAUDE_PING,
            'max_tokens': 10,
            'messages': [{'role': 'user', 'content': 'hi'}]
        }).encode('utf-8')
        req = urllib.request.Request(
            CLAUDE_API_URL, data=payload,
            headers={'Content-Type': 'application/json',
                     'x-api-key': key,
                     'anthropic-version': '2023-06-01'},
            method='POST'
        )
        _ctx = ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=15, context=_ctx) as resp:
            resp.read()
        _send('success', 'API key תקין ✓')
        if _palette:
            _palette.sendInfoToHTML('key_ok', '{}')
    except urllib.error.HTTPError as e:
        code = e.code
        body = e.read().decode('utf-8', errors='replace')
        err_detail = ''
        try:
            err_detail = json.loads(body).get('error', {}).get('message', body[:120])
        except Exception:
            err_detail = body[:120]

        if code == 401:
            _send('error', f'API key שגוי (401): {err_detail}')
            if _palette:
                _palette.sendInfoToHTML('key_err', '{}')
        elif code == 429:
            _send('system', 'Rate limit — המפתח תקין, הגעת למגבלה זמנית')
            if _palette:
                _palette.sendInfoToHTML('key_ok', '{}')
        else:
            _send('error', f'API error {code}: {err_detail}')
            if _palette:
                _palette.sendInfoToHTML('key_err', '{}')
    except Exception as e:
        err_str = str(e)
        if 'getaddrinfo' in err_str or 'Name or service' in err_str or 'timed out' in err_str or 'Connection refused' in err_str:
            _send('error', f'בעיית רשת — לא ניתן להגיע ל-Anthropic API. בדוק חיבור אינטרנט/חומת אש. ({err_str})')
            # Key might still be valid — don't mark as invalid
        else:
            _send('error', f'בדיקה נכשלה: {err_str}')
            if _palette:
                _palette.sendInfoToHTML('key_err', '{}')


def _test_groq_key_thread(params):
    key = params.get('key', _groq_api_key).strip()
    if not key:
        _send('error', 'הכנס Groq API key תחילה.')
        return
    _send('system', 'בודק Groq key...')
    try:
        payload = json.dumps({
            'model': GROQ_MODEL,
            'max_tokens': 10,
            'messages': [{'role': 'user', 'content': 'hi'}],
        }).encode('utf-8')
        _http_post(GROQ_API_URL, payload, {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {key}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })
        _send('success', 'Groq key תקין ✓')
        if _palette:
            _palette.sendInfoToHTML('groq_key_ok', '{}')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            msg = json.loads(body).get('error', {}).get('message', body[:100])
        except Exception:
            msg = body[:100]
        _send('error', f'Groq error {e.code}: {msg}')
        if _palette:
            _palette.sendInfoToHTML('groq_key_err', '{}')
    except Exception as e:
        _send('error', f'Groq: {e}')


def _test_gemini_key_thread(params):
    key = params.get('key', _gemini_api_key).strip()
    if not key:
        _send('error', 'הכנס Gemini API key תחילה.')
        return
    _send('system', 'בודק Gemini key...')
    try:
        payload = json.dumps({
            'contents': [{'role': 'user', 'parts': [{'text': 'hi'}]}],
            'generationConfig': {'maxOutputTokens': 10},
        }).encode('utf-8')
        url = f'{f'{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent'}?key={key}'
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        _send('success', 'Gemini key תקין ✓')
        if _palette:
            _palette.sendInfoToHTML('gemini_key_ok', '{}')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            msg = json.loads(body).get('error', {}).get('message', body[:120])
        except Exception:
            msg = body[:120]
        if e.code == 429:
            _send('system', 'Gemini key תקין ✓ (rate limit זמני — נסה שוב בעוד כמה שניות)')
            if _palette:
                _palette.sendInfoToHTML('gemini_key_ok', '{}')
        else:
            _send('error', f'Gemini error {e.code}: {msg}')
            if _palette:
                _palette.sendInfoToHTML('gemini_key_err', '{}')
    except Exception as e:
        _send('error', f'Gemini: {e}')


def _get_ollama_models_thread(_params=None):
    """Fetch installed Ollama models from /api/tags."""
    try:
        req = urllib.request.Request(
            'http://localhost:11434/api/tags',
            headers={'Content-Type': 'application/json'},
            method='GET'
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        models = [m['name'] for m in body.get('models', [])]
        if not models:
            _send('system', 'Ollama פועל אך אין מודלים מותקנים. הרץ: ollama pull qwen2.5-coder:7b')
            return
        if _palette:
            _palette.sendInfoToHTML('ollama_models', json.dumps({'models': models}))
        _send('system', f'Ollama: נמצאו {len(models)} מודלים')
    except urllib.error.URLError:
        _send('system', 'Ollama לא פועל (localhost:11434) — הפעל Ollama תחילה')
    except Exception as e:
        _send('system', f'Ollama detection: {e}')


# ══════════════════════════════════════════════════════════════
# ASSEMBLY PIPELINE
# ══════════════════════════════════════════════════════════════

ASSEMBLY_SYSTEM_PROMPT = r"""You are an expert Autodesk Fusion 360 API (Python) developer. The user describes a part or assembly; you return ONE complete build_assembly function. The code MUST run without InternalValidationError, without NoneType/'bodies' errors, and MUST produce correct, NON-FLOATING, production-quality geometry.

== OUTPUT FORMAT (this app's execution model) ==
Return ONE function in a single ```python block:
```python
def build_assembly(rootComp):
    import adsk.core, adsk.fusion, math, traceback
    # app, ui, design, rootComp are ALL already available as globals. Do NOT call documents.add().
    def cm(mm): return mm / 10.0   # Fusion API is in CM; define params in mm, convert with cm()
    current_step = 'start'
    bodies_made = []
    try:
        # ... build EVERY part here, sharing variables so they fit together ...
        return {"parts": [b.name for b in bodies_made], "bodies": len(bodies_made)}
    except Exception as e:
        raise RuntimeError('Failed at step "{}": {}'.format(current_step, e))
```
Build ALL parts inside this ONE function so they share variables and mate. Update
`current_step` (a short string) before each major operation so a failure reports the exact step.

== UNITS ==
Fusion's API is in CENTIMETERS. Define every dimension as a named variable in MM in one block
at the top, then wrap EVERY value passed to ValueInput.createByReal / Point3D.create with cm().
Never pass a raw mm number to the API.

== ANTI-CRASH RULES ==
1. NEVER chain attribute access on an API call result. Capture, verify, then use.
   BAD:  body = extrudes.add(extInput).bodies.item(0)
   GOOD: ext = extrudes.add(extInput)
         if not ext or ext.bodies.count == 0: raise RuntimeError("extrude produced no body")
         body = ext.bodies.item(0)
2. Validate EVERY profile before extrude/revolve:
         if sketch.profiles.count == 0: raise RuntimeError("no closed profile: <name>")
   Pick the intended profile explicitly (by area/index); never blindly item(0) on a multi-loop sketch.
3. Revolve axis must be a real sketch line / construction axis that does NOT pass through the profile. Verify isValid.
4. Use ValueInput.createByReal (cm) for all values; set explicit operation enums
   (NewBodyFeatureOperation / JoinFeatureOperation / CutFeatureOperation). After each feature, assert it succeeded.
5. Boolean/combine: verify both target and tool bodies exist and isValid; wrap tools in an ObjectCollection with count>0.
6. Re-capture faces by GEOMETRY (Cylinder/Plane + area/normal), never by hard-coded index, after each feature.

== ANTI-FLOAT / ASSEMBLY RULES ==
7. Define a SINGLE reference datum axis + origin at the top. EVERY body is positioned relative to it. No body "in mid-air".
8. All parts sharing a rotation axis (hinge knuckles, bearing rings+balls, pins) MUST be concentric to the SAME datum axis, computed once. Never two separate axes for parts that share one.
9. Every derived dimension is a named variable computed in code (segment length, pitch circle, groove radius, ball Z-center, bolt circle, axial seat Z, knuckle center). No inline magic numbers implying geometry.
10. Seating: when one body sits in/on another (bearing in bore, leaf on axis), position it by the CONTACT value (shoulder-face Z, bore wall), not an arbitrary coordinate.

== HINGE-SPECIFIC ==
11. seg = length / N_segments. Leaf A owns even segments, leaf B owns odd (interleaved), with a named axial clearance. NEVER one continuous knuckle. Create BOTH leaves.
12. The two leaves extend from the hinge axis in OPPOSITE directions (opening default 180deg => flat, both leaves in one straight plane). They must NEVER overlap or be on the same side.
13. Each leaf's mid-thickness plane aligns to the hinge-axis height, so opened flat they form one continuous plane. The knuckle barrel axis is ON the hinge axis at leaf mid-thickness; barrel_dia >= 2*leaf_thickness.
14. The pin spans all segments on the datum axis; pin length >= total knuckle length.

== BEARING-SPECIFIC ==
15. Place the bearing flat on a defined shoulder at an explicit Z. Compute pitch circle, ball count, ball diameter, raceway groove radius, ball Z-center. Balls sit inside grooves, not floating. Outer ring fits the bore; inner ring bore is the shaft size. (A simplified bearing = two concentric rings with a gap is acceptable and more reliable.)

== PRODUCTION FINISH (wrap EACH in try/except so a finish never breaks the part) ==
16. Chamfers/fillets 0.4-0.6 mm on knuckle ends, cylinder mouths, sharp outer edges.
17. Pins get a head on one end (head dia > hole dia) + a chamfer on the other.
18. Screw holes countersunk where appropriate (count, hole dia, countersink dia + angle).
19. Axial clearance 0.2-0.3 mm between adjacent segments; radial clearance ~0.2 mm pin-to-bore.

== VALIDATION ==
20. Count bodies created vs expected (hinge = leafA+leafB+pin = 3). If fewer, raise RuntimeError naming the missing body.
21. Assert no created body is None and each isValid; append every created body to bodies_made.

== STRUCTURE SUMMARY ==
- comp = rootComp; build all bodies into it; do NOT use addNewComponent or Matrix3D transforms.
- sketch.isComputeDeferred = True at sketch start, False before reading .profiles.
- Return ONLY the complete def build_assembly(rootComp) in ONE ```python block. No prose."""


ASSEMBLY_MOTION_SYSTEM_PROMPT = r"""You are an expert Autodesk Fusion 360 API (Python) developer building REAL ASSEMBLIES with moving parts. Every mechanical part is a separate COMPONENT (not a body under root), connected by JOINTS so it actually moves in Fusion.

== OUTPUT FORMAT ==
Return ONE function in a single ```python block:
```python
def build_assembly(rootComp):
    import adsk.core, adsk.fusion, math, traceback
    # adsk, app, ui, design, rootComp are available as globals. Do NOT call documents.add().
    def cm(mm): return mm / 10.0   # Fusion API is in CM; define dims in mm, convert with cm()
    step = 'start'
    try:
        # ... create components, build geometry inside them, add joints, ground, drive ...
        return {"components": [...names...], "joints": [...{"type":..,"between":..}..],
                "grounded": "...", "moving": "...", "drive_ok": True}
    except Exception as e:
        raise RuntimeError('Failed at step "{}": {}'.format(step, e))
```

== RULE 1 — REAL COMPONENTS (critical) ==
Each independent mechanical part MUST be its own Component, NOT a body under root.
WRONG: root.sketches.add(...)   /   root.features.extrudeFeatures
RIGHT:
    occ = rootComp.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = "Leaf_A"
    sk = comp.sketches.add(comp.xYConstructionPlane)
    ext = comp.features.extrudeFeatures
ALL sketches, construction planes, extrudes, revolves, holes, combines for a part
go INSIDE that part's component. Keep both the occurrence (occ) and component (comp)
references — you need the OCCURRENCE for joints/grounding.
Result tree: Root -> Leaf_A:1 / Leaf_B:1 / Pin:1 (each with its own Bodies). NEVER put bodies under Root.

== RULE 2 — POSITION + DATUM ==
Define a single datum axis + origin at the top. Build each component's geometry at its
real assembled position relative to that datum (parts that share a rotation axis are
concentric to the SAME axis, computed once). Parts must touch, not float.

== RULE 3 — JOINTS (the motion) — USE AS-BUILT JOINTS ==
You MUST use rootComp.asBuiltJoints — they join components in their CURRENT built
position (parts stay put). NEVER use rootComp.joints with JointGeometry.createByCurve /
createByPoint — that relocates parts and scatters them into a 'cross' shape. FORBIDDEN.
As-built revolute joint (the reliable pattern):
    ab  = rootComp.asBuiltJoints
    jin = ab.createInput(occ_moving, occ_fixed, None)   # None geometry = join where they are
    joint = ab.add(jin)
    joint.setAsRevoluteJointMotion(adsk.fusion.JointDirections.XAxisJointDirection)  # axis dir
Motion types (call on the JOINT after add): setAsRigidJointMotion() / setAsRevoluteJointMotion(dir) /
setAsSliderJointMotion(dir) / setAsCylindricalJointMotion(dir) / setAsPlanarJointMotion(dir,n) / setAsBallJointMotion().
Choose the JointDirection (X/Y/Z) to MATCH the real axis (pin/shaft direction).
Wrap each joint in try/except that re-raises with a clear step name — the assembly is only a
success if joints exist, so do NOT silently swallow.

== RULE 4 — GROUND, LIMITS, DRIVE ==
- Ground the base component:  occ_base.isGrounded = True
- Limits (revolute, radians):
    rm = joint.jointMotion
    rm.rotationLimits.isMinimumValueEnabled = True; rm.rotationLimits.minimumValue = 0
    rm.rotationLimits.isMaximumValueEnabled = True; rm.rotationLimits.maximumValue = math.radians(180)
- Drive to a test angle to prove it moves:  rm.rotationValue = math.radians(90)
  (for slider use slideValue, for cylindrical rotationValue/slideValue) — wrap in try/except.

== HINGE RECIPE (follow exactly when asked for a hinge/ציר) ==
- Leaf_A, Leaf_B, Pin are THREE separate components.
- Hinge axis = pin centerline; both leaves' inner knuckles are concentric to it.
- Leaf_A and Leaf_B extend in OPPOSITE directions from the axis (flat = 180deg apart).
- Interleaved knuckles: Leaf_A even segments, Leaf_B odd, axial clearance ~0.2-0.3mm.
- Ground Leaf_A. Rigid joint Pin<->Leaf_A. Revolute joint Leaf_B<->Leaf_A about the pin axis.
- Limits 0..180deg; drive to 90deg to test.

== VALIDATION (do this at the END, before returning) ==
- Count components you created; if fewer than expected, raise RuntimeError naming the missing one.
- If the request needs motion, assert at least one NON-RIGID joint was created; else raise.
- Assert no body was created directly under rootComp (rootComp.bRepBodies.count == 0).
Return the structured dict (components, joints with types, grounded, moving, drive_ok).

== FINISH ==
Chamfers/fillets 0.4-0.6 mm on knuckle ends and cylinder mouths (each in its own try/except).
Pin gets a head one end + chamfer the other. Add 0.2mm radial clearance pin-to-bore.

Return ONLY the complete def build_assembly(rootComp) in ONE ```python block. No prose."""


def _real_assembly_pipeline_thread(params):
    """Build a REAL assembly: separate Components + Joints (it moves in Fusion).
    Validates the result (components, joints, motion) and only reports success
    if it genuinely passes."""
    global _last_code, _last_params
    raw = params.get('text', '')
    if not raw:
        _send('error', 'תאר את ההרכבה')
        _send_progress(0, 0)
        return

    need_motion = _is_motion_assembly(raw)
    prompt = _maybe_expand(raw, params)
    motion_line = ('This mechanism MUST MOVE — create the right joints (revolute/slider/...), '
                   'ground the base component, set limits, and drive to a test angle.'
                   if need_motion else 'Create each part as a separate component.')
    full = """Build a REAL Fusion 360 assembly (separate Components + Joints) for:
{}

Material: {}
{}""".format(prompt, params.get('material', 'Steel'), motion_line)

    _send('system', '[1/2] AI מייצר הרכבה אמיתית (Components + Joints)...')
    _send_progress(1, 2, 'Generating real assembly...')
    try:
        resp = _call_claude(ASSEMBLY_MOTION_SYSTEM_PROMPT, [{'role': 'user', 'content': full}])
    except Exception as e:
        _send('error', 'AI error: {}'.format(e))
        _send_progress(0, 0)
        return

    code = _extract_python(resp, 'def build_assembly(')
    if not code or 'def build_assembly(' not in code:
        _send('error', 'AI לא החזיר build_assembly(). נסה שוב.')
        _send_progress(0, 0)
        return
    if params.get('show_code'):
        _send('code', code[:2000] + ('...' if len(code) > 2000 else ''))

    _send('system', '[2/2] בונה Components + Joints ב-Fusion...')
    _send_progress(2, 2, 'Building real assembly...')
    res = _fire_and_wait({'mode': 'assembly_real', 'code': code, 'need_motion': need_motion}, timeout=150)

    if not res.get('ok'):
        _send('system', 'נכשל ({}) — מתקן...'.format(res.get('msg', '')[:140]))
        try:
            fix = _call_claude(ASSEMBLY_MOTION_SYSTEM_PROMPT, [
                {'role': 'user', 'content': full},
                {'role': 'assistant', 'content': resp},
                {'role': 'user', 'content': 'FAILED:\n{}\nFix and return the complete def build_assembly(rootComp).'.format(res.get('msg', '')[:600])},
            ])
            fc = _extract_python(fix, 'def build_assembly(')
            if fc:
                code = fc
                res = _fire_and_wait({'mode': 'assembly_real', 'code': fc, 'need_motion': need_motion}, timeout=150)
        except Exception:
            pass

    if res.get('ok'):
        _last_code = code
        shot = _fire_and_wait({'mode': 'screenshot'}, timeout=10)
        if shot.get('ok') and shot.get('b64') and _palette:
            _palette.sendInfoToHTML('screenshot', json.dumps({'b64': shot['b64']}))
        info = res.get('info', {})
        names = ', '.join(info.get('components', [])) if isinstance(info.get('components'), list) else ''
        report = (
            'ASSEMBLY REPORT\n'
            '================\n'
            'Components: {}   {}\n'.format(res.get('components', '?'), names) +
            'Joints: {}  (non-rigid: {})\n'.format(res.get('joints', '?'), res.get('nonrigid', '?')) +
            'Grounded: {}   Moving: {}\n'.format(info.get('grounded', '?'), info.get('moving', '?')) +
            'Drive joint: {}\n'.format('available' if info.get('drive_ok') else '—') +
            'Bodies under Root: {} (must be 0)'.format(res.get('root_bodies', '?'))
        )
        _send('code', report)
        if res.get('nonrigid', 0) > 0:
            _send('done', '🎬 הרכבה חיה מוכנה! גרור חלק או Animate Joint ב-Fusion כדי לראות תנועה.')
        else:
            _send('done', 'הרכבה מוכנה (Components נפרדים, ללא תנועה).')
    else:
        _send('error', 'הרכבה נכשלה: {}'.format(res.get('msg', '')[:400]))
    _send_progress(0, 0)


def _assembly_pipeline_thread(params):
    """Build a multi-part assembly as ONE coherent function so the parts share a
    coordinate frame and actually fit together (instead of floating apart)."""
    global _last_code, _last_params

    prompt   = params.get('text', '')
    material = params.get('material', 'Steel')
    process  = params.get('process', 'CNC Machining')
    units    = params.get('units', 'mm')
    show_code = params.get('show_code', False)

    # Auto-detail: expand a short assembly request into a full engineering spec.
    prompt = _maybe_expand(prompt, params)

    full_prompt = f"""Create a Fusion 360 assembly for:

DESCRIPTION: {prompt}
Material: {material}
Manufacturing process: {process}
Units: {units}

All parts MUST be positioned to FIT TOGETHER — touching, correctly oriented, no gaps.
Return ONE def build_assembly(rootComp) that builds the whole assembly in one pass."""

    _send('system', '[1/2] AI מייצר הרכבה...')
    _send_progress(1, 2, 'Generating assembly...')
    try:
        response = _call_claude(ASSEMBLY_SYSTEM_PROMPT, [{'role': 'user', 'content': full_prompt}])
    except Exception as e:
        _send('error', f'AI error: {e}')
        _send_progress(0, 0)
        return

    code = _extract_python(response, 'def build_assembly(')
    if not code or 'def build_assembly(' not in code:
        _send('error', 'AI לא החזיר build_assembly(). נסה שוב או פשט את התיאור.')
        _send_progress(0, 0)
        return
    if show_code:
        _send('code', code[:2000] + ('...' if len(code) > 2000 else ''))

    # Wrap as build() and run the WHOLE assembly in a SINGLE exec — this is what
    # lets the parts share coordinates and mate correctly.
    def _wrap(c):
        return c + '\n\ndef build(rootComp, config):\n    return build_assembly(rootComp)\n'

    _send('system', '[2/2] בונה הרכבה ב-Fusion...')
    _send_progress(2, 2, 'Building assembly...')
    res = _fire_and_wait({'mode': 'model', 'code': _wrap(code)}, timeout=120)

    if not res.get('ok'):
        _send('system', f'נכשל — מנסה לתקן ({res.get("msg","")[:120]})...')
        try:
            fix = _call_claude(ASSEMBLY_SYSTEM_PROMPT, [
                {'role': 'user', 'content': full_prompt},
                {'role': 'assistant', 'content': response},
                {'role': 'user', 'content': f"FAILED with:\n{res.get('msg','')[:600]}\nFix it and return the complete def build_assembly(rootComp)."},
            ])
            fix_code = _extract_python(fix, 'def build_assembly(')
            if fix_code:
                code = fix_code
                res = _fire_and_wait({'mode': 'model', 'code': _wrap(fix_code)}, timeout=120)
        except Exception:
            pass

    if res.get('ok'):
        _last_code = code
        info = res.get('info', {})
        _last_params = info.get('params', {})
        if info.get('params'):
            _send_params(info['params'])
        shot = _fire_and_wait({'mode': 'screenshot'}, timeout=10)
        if shot.get('ok') and shot.get('b64') and _palette:
            _palette.sendInfoToHTML('screenshot', json.dumps({'b64': shot['b64']}))
        _send('done', 'הרכבה מוכנה!')
    else:
        _send('error', f'הרכבה נכשלה: {res.get("msg", "")[:400]}')
    _send_progress(0, 0)


def _weight_opt_thread(params):
    material = params.get('material', 'Steel')
    process  = params.get('process', 'CNC Machining')

    if not _last_params:
        _send('error', 'No model parameters found. Build a model first.')
        return

    _send('system', 'Analyzing weight optimization opportunities...')
    mat_props = MATERIAL_PROPS.get(material, {})

    msg = f"""Part parameters:
{json.dumps({k: v['expression'] for k, v in _last_params.items()}, indent=2)[:800]}

Material: {material} (density: {mat_props.get('density', '?')} g/cm³, yield: {mat_props.get('yield', '?')} MPa)
Manufacturing process: {process}

Suggest specific parameter changes to reduce weight by 20-30% while maintaining structural integrity.
For each suggestion: state the parameter name, current value, suggested value, and estimated weight saving.
Also suggest if shell/pocket operations should be added."""

    try:
        resp = _simple_call(WEIGHT_OPT_PROMPT, msg, max_tokens=2048)
        _send('system', 'Weight optimization suggestions:')
        _send('code', resp[:2000])
    except Exception as e:
        _send('error', f'Optimization error: {e}')


def _query_pipeline_thread(params):
    """Answer a natural-language question about the current model."""
    question = params.get('text', '')
    if not question:
        _send('error', 'שאלה ריקה')
        return

    _send('system', 'קורא נתוני מודל...')
    ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
    if not ctx_res.get('ok'):
        _send('error', 'לא ניתן לקרוא מודל פעיל')
        return
    ctx = ctx_res.get('context', {})
    if 'error' in ctx:
        _send('error', f'אין מודל פעיל: {ctx["error"]}')
        return

    bodies_count = ctx.get('body_count', 0)
    params_count = len(ctx.get('parameters', {}))
    feats_count  = len(ctx.get('features', []))
    _send('system', f'מודל: {ctx.get("component_name","?")} — {bodies_count} גופים, {params_count} פרמטרים, {feats_count} פיצ\'רים')

    use_api = (_provider != 'ollama')   # claude / groq / gemini all route via _call_ai
    user_msg = f"""Model context (JSON):
{json.dumps(ctx, indent=2, ensure_ascii=False)[:3000]}

User question: {question}

Write def query(design, rootComp) that reads the relevant data and returns the answer as a Hebrew string."""

    try:
        if use_api:
            response = _call_ai(QUERY_SYSTEM_PROMPT, [{'role': 'user', 'content': user_msg}], max_tokens=1024)
        else:
            response = _call_ollama(user_msg, system=QUERY_SYSTEM_PROMPT, max_tokens=1024)
    except Exception as e:
        _send('error', f'AI error: {e}')
        return

    code = _extract_python(response, 'def query(')
    if not code:
        # AI answered directly as text
        _send('done', response[:800])
        return

    _send('system', 'מריץ שאילתה...')
    res = _fire_and_wait({'mode': 'run_query', 'code': code}, timeout=15)
    if res.get('ok'):
        _send('done', res.get('answer', '(אין תשובה)'))
    else:
        _send('error', f'שגיאה: {res.get("msg", "")}')


def _edit_pipeline_thread(params):
    """AI-driven modification of the existing model."""
    request = params.get('text', '')
    if not request:
        _send('error', 'תאר מה לשנות')
        return

    _send('system', 'מצב עריכה — קורא מודל קיים...')
    ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
    if not ctx_res.get('ok'):
        _send('error', 'לא ניתן לקרוא מודל פעיל. בנה מודל קודם.')
        return
    ctx = ctx_res.get('context', {})
    if 'error' in ctx or ctx.get('body_count', 0) == 0:
        _send('error', 'אין גוף פעיל לעריכה. צור מודל קודם.')
        return

    use_api = (_provider != 'ollama')   # claude / groq / gemini all route via _call_ai
    user_msg = f"""Model context:
{json.dumps(ctx, indent=2, ensure_ascii=False)[:2000]}

Existing build code:
```python
{_last_code[:1000] if _last_code else '# (no previous code)'}
```

Edit request: {request}

Write def modify(design, rootComp, timeline_mark) to apply this change."""

    _send('system', 'AI מייצר קוד עריכה...')
    try:
        if use_api:
            response = _call_ai(EDIT_SYSTEM_PROMPT, [{'role': 'user', 'content': user_msg}], max_tokens=3000)
        else:
            response = _call_ollama(user_msg, system=EDIT_SYSTEM_PROMPT, max_tokens=3000)
    except Exception as e:
        _send('error', f'AI error: {e}')
        return

    code = _extract_python(response, 'def modify(')
    if not code:
        _send('error', 'AI לא החזיר פונקציית modify(). נסה לנסח מחדש.')
        return

    _send('system', 'מריץ עריכה ב-Fusion...')
    res = _fire_and_wait({'mode': 'run_edit', 'code': code, 'timeline_mark': _last_timeline_mark}, timeout=60)

    if res.get('ok'):
        info = res.get('info', {})
        desc = info.get('description', 'שינוי בוצע') if isinstance(info, dict) else str(info)
        _send('success', f'עריכה הושלמה: {desc}')
        shot = _fire_and_wait({'mode': 'screenshot'}, timeout=10)
        if shot.get('ok') and shot.get('b64') and _palette:
            _palette.sendInfoToHTML('screenshot', json.dumps({'b64': shot['b64']}))
    else:
        rolled = ' (הוחזר לאחור)' if res.get('rolled_back') else ''
        _send('error', f'עריכה נכשלה{rolled}: {res.get("msg", "")}')
    _send_progress(0, 0)   # reset busy — success path sends 'success', which doesn't unlock


def _params_pipeline_thread(params):
    """Create/list/modify user parameters via natural language."""
    request = params.get('text', '')
    if not request:
        _send('error', 'תאר מה לעשות עם הפרמטרים')
        return

    use_api = (_provider != 'ollama')   # claude / groq / gemini all route via _call_ai
    _send('system', 'מפענח בקשת פרמטר...')

    try:
        if use_api:
            raw = _call_ai(PARAM_PARSE_PROMPT, [{'role': 'user', 'content': request}], max_tokens=256)
        else:
            raw = _call_ollama(request, system=PARAM_PARSE_PROMPT, max_tokens=256)
    except Exception as e:
        _send('error', f'AI error: {e}')
        return

    # Extract JSON
    parsed = None
    try:
        import re as _re
        m = _re.search(r'\{.*?\}', raw, _re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
    except Exception:
        pass
    if not parsed:
        _send('error', f'לא הצלחתי לפענח תגובת AI: {raw[:200]}')
        return

    action_type = parsed.get('action', 'list')

    if action_type == 'list':
        res = _fire_and_wait({'mode': 'manage_params', 'action_type': 'list'}, timeout=10)
        if res.get('ok'):
            rows = res.get('params', [])
            if rows:
                _send_params({r['name']: {'value': round(r['value']*10,4), 'unit': 'mm', 'expression': r['expression']} for r in rows})
                _send('system', f'נמצאו {len(rows)} פרמטרים')
            else:
                _send('system', 'אין פרמטרים במודל הנוכחי')
        else:
            _send('error', res.get('msg', ''))

    elif action_type == 'set':
        name = _sanitize_param_name(parsed.get('name', 'param'))
        expr = parsed.get('expression', '0')
        unit = parsed.get('unit', 'cm')
        comment = parsed.get('comment', '')
        res = _fire_and_wait({
            'mode': 'manage_params',
            'action_type': 'set',
            'name': name,
            'expression': expr,
            'unit': unit,
            'comment': comment,
        }, timeout=10)
        if res.get('ok'):
            _send('success', res.get('msg', 'בוצע'))
        else:
            _send('error', res.get('msg', ''))
    else:
        _send('error', f'פעולה לא מוכרת: {action_type}')
    _send_progress(0, 0)   # reset busy — 'success'/'system' paths don't unlock the UI


def _mfg_analysis_thread(params):
    """Analyze current model for manufacturing."""
    request = params.get('text', '')
    process = params.get('process', 'CNC Machining')

    _send('system', f'ניתוח ייצור ({process})...')
    ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
    if not ctx_res.get('ok'):
        _send('error', 'לא ניתן לקרוא מודל פעיל')
        return
    ctx = ctx_res.get('context', {})
    if 'error' in ctx:
        _send('error', f'אין מודל: {ctx["error"]}')
        return

    use_api = (_provider != 'ollama')   # claude / groq / gemini all route via _call_ai
    user_msg = f"""Manufacturing process: {process}
User question: {request if request else 'נתח את החלק עבור תהליך הייצור'}

Model data:
{json.dumps(ctx, indent=2, ensure_ascii=False)[:3000]}

Provide detailed manufacturing analysis in Hebrew."""

    try:
        if use_api:
            answer = _call_ai(MANUFACTURING_PROMPT, [{'role': 'user', 'content': user_msg}], max_tokens=2048)
        else:
            answer = _call_ollama(user_msg, system=MANUFACTURING_PROMPT, max_tokens=2048)
        _send('analysis', answer)
    except Exception as e:
        _send('error', f'שגיאת AI: {e}')
    _send_progress(0, 0)   # reset busy — 'analysis' alone doesn't unlock the UI


_GDT_SYMBOLS = {
    'flatness': '⏥', 'straightness': '⏤', 'circularity': '○', 'cylindricity': '⌭',
    'parallelism': '∥', 'perpendicularity': '⊥', 'angularity': '∠', 'position': '⊕',
    'concentricity': '◎', 'symmetry': '⌯', 'runout': '↗', 'total_runout': '↗↗',
    'profile_line': '⌒', 'profile_surface': '⌓',
}


def _format_gdt_report(gdt, part_name=''):
    """Render the GD&T JSON spec as a readable monospace report."""
    L = [f'GD&T REPORT  —  {part_name or "Part"}', '=' * 40]

    datums = gdt.get('datums', [])
    if datums:
        L.append(f'\nDATUMS ({len(datums)}):')
        for d in datums:
            L.append(f"  [{d.get('label','?')}] {d.get('feature','?')} — {d.get('description','')}")

    fcs = gdt.get('feature_controls', [])
    if fcs:
        L.append(f'\nFEATURE CONTROL FRAMES ({len(fcs)}):')
        for fc in fcs:
            sym = _GDT_SYMBOLS.get(fc.get('symbol', ''), fc.get('symbol', '?'))
            dia = '⌀' if fc.get('diameter_zone') else ''
            mmc = ' (M)' if fc.get('mmc') else (' (L)' if fc.get('lmc') else '')
            refs = ' | '.join(fc.get('datums', []))
            refs = f'  [{refs}]' if refs else ''
            L.append(f"  {sym}  {dia}{fc.get('tolerance','?')}{mmc}{refs}  →  {fc.get('feature','?')}")
            if fc.get('description'):
                L.append(f"        {fc['description']}")

    sf = gdt.get('surface_finishes', [])
    if sf:
        L.append('\nSURFACE FINISH:')
        for s in sf:
            L.append(f"  {s.get('feature','?')}: Ra {s.get('ra_um','?')} µm  ({s.get('process','')})")

    dt = gdt.get('dimensional_tolerances', [])
    if dt:
        L.append('\nDIMENSIONAL TOLERANCES:')
        for t in dt:
            fit = f" {t.get('fit','')}" if t.get('fit') else ''
            up, lo = t.get('upper'), t.get('lower')
            tol = f"  (+{up} / {lo})" if up is not None and lo is not None else ''
            L.append(f"  {t.get('feature','?')}: ⌀{t.get('nominal','?')}{fit}{tol}")

    gt = gdt.get('general_tolerance')
    if gt:
        L.append(f'\nGENERAL TOLERANCE: {gt}')

    return '\n'.join(L)


def _gdt_drawing_pipeline_thread(params):
    """Produce a GD&T analysis report for the CURRENT model.

    NOTE: Fusion's API CANNOT create 2D drawings programmatically — it is a
    documented platform limitation (none of the drawing functionality is exposed
    through the API). So this generates a GD&T specification report instead of a
    drawing. For an actual sheet, use Fusion's native 'Drawing from Design'.
    """
    description = params.get('text', '')

    _send('system', 'קורא נתוני מודל...')
    ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
    if not ctx_res.get('ok'):
        _send('error', 'לא ניתן לקרוא מודל פעיל. פתח מודל קודם.')
        _send_progress(0, 0)
        return
    ctx = ctx_res.get('context', {})
    if 'error' in ctx or ctx.get('body_count', 0) == 0:
        _send('error', 'אין גוף פעיל. צור מודל קודם.')
        _send_progress(0, 0)
        return

    params_dict = ctx.get('parameters', {})
    body_names = [b['name'] for b in ctx.get('bodies', [])]
    _send('system', f'מודל: {ctx.get("component_name","?")} — גופים: {", ".join(body_names)}')

    ctx_text = f"""Part name: {ctx.get('component_name', 'Unknown')}
Bodies: {json.dumps(body_names)}
Parameters: {json.dumps({k: v['expression'] for k, v in params_dict.items()}, ensure_ascii=False)[:600]}
Features: {json.dumps([f['type'] for f in ctx.get('features', [])[:20]])}
{f'Additional description: {description}' if description else ''}"""

    _send_progress(1, 1, 'מנתח GD&T...')
    _send('system', 'מנתח גיאומטריה וקובע callouts של GD&T...')
    try:
        gdt_msg = f"""Determine GD&T callouts for this part:
{ctx_text}

Return ONLY a JSON specification with: datums, feature_controls, surface_finishes, dimensional_tolerances, general_tolerance."""
        gdt_resp = _simple_call(GDT_SYSTEM_PROMPT, gdt_msg)
        gdt_data = _extract_json(gdt_resp)
        if gdt_data:
            report = _format_gdt_report(gdt_data, ctx.get('component_name', ''))
            _send('code', report)   # monospace block keeps the columns aligned
            nd = len(gdt_data.get('datums', []))
            nc = len(gdt_data.get('feature_controls', []))
            _send('success', f'דוח GD&T מוכן — {nd} datums, {nc} feature controls ✓')
        else:
            _send('error', 'לא ניתן לפרסר את תגובת ה-GD&T. נסה שוב.')
    except Exception as e:
        _send('error', f'שגיאת GD&T: {e}')

    _send_progress(0, 0)


def _explain_timeline_thread(params):
    """Explain the current model's timeline in plain Hebrew."""
    _send('system', 'קורא ציר הזמן...')
    ctx_res = _fire_and_wait({'mode': 'get_context'}, timeout=15)
    if not ctx_res.get('ok'):
        _send('error', 'לא ניתן לקרוא מודל')
        return
    ctx = ctx_res.get('context', {})
    if 'error' in ctx:
        _send('error', f'אין מודל: {ctx["error"]}')
        return

    features = ctx.get('features', [])
    params_dict = ctx.get('parameters', {})

    if not features:
        _send('system', 'ציר הזמן ריק — אין פיצ\'רים להסביר.')
        _send_progress(0, 0)
        return

    _send('system', f'מסביר {len(features)} פיצ\'רים...')

    use_api = (_provider != 'ollama')   # claude / groq / gemini all route via _call_ai
    user_msg = f"""הסבר את המודל הבא בעברית פשוטה:

שם: {ctx.get('component_name', '?')}
גופים: {json.dumps([b['name'] for b in ctx.get('bodies', [])], ensure_ascii=False)}
פרמטרים: {json.dumps({k: v['expression'] for k, v in params_dict.items()}, ensure_ascii=False)[:400]}

פיצ'רים בציר הזמן:
{json.dumps(features, indent=2, ensure_ascii=False)[:2000]}

הסבר בעברית:
1. מה המוצר/החלק הזה?
2. מה עשה כל שלב בציר הזמן (בשפה פשוטה)?
3. מה הפרמטרים החשובים?
4. לאיזה שימוש מתאים החלק?

היה קצר וברור."""

    try:
        system = "אתה מומחה CAD שמסביר מודלים של Fusion 360 בעברית פשוטה לכל מי שישאל אותך."
        if use_api:
            answer = _call_ai(system, [{'role': 'user', 'content': user_msg}], max_tokens=1024)
        else:
            answer = _call_ollama(user_msg, system=system, max_tokens=1024)
        _send('analysis', answer)
    except Exception as e:
        _send('error', f'שגיאת AI: {e}')
    _send_progress(0, 0)   # reset busy — 'analysis' alone doesn't unlock the UI


# ══════════════════════════════════════════════════════════════
# HTML EVENT HANDLER
# ══════════════════════════════════════════════════════════════

class HTMLEventHandler(adsk.core.HTMLEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        global _provider, _claude_api_key, _ollama_model, _conversation, _last_code, _last_params
        global _confirm_event, _confirm_result

        try:
            action = args.action
            data   = json.loads(args.data) if args.data else {}

            if action == 'send_message':
                t = threading.Thread(target=_pipeline_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'analyze_image':
                if not _claude_api_key:
                    _send('error', 'Claude API key required for image analysis.')
                    return
                t = threading.Thread(target=_image_pipeline_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'weight_opt':
                t = threading.Thread(target=_weight_opt_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'export':
                fmt = data.get('fmt', 'step')
                def _exp():
                    res = _fire_and_wait({'mode': 'export', 'fmt': fmt}, timeout=30)
                    if res.get('ok'):
                        _send('success', f'{fmt.upper()} exported: {res["msg"]}')
                    else:
                        _send('error', f'Export failed: {res.get("msg", "")}')
                threading.Thread(target=_exp, daemon=True).start()

            elif action == 'bom':
                def _bom():
                    res = _fire_and_wait({'mode': 'bom'}, timeout=10)
                    _send('code', res.get('msg', 'No BOM'))
                threading.Thread(target=_bom, daemon=True).start()

            elif action == 'delete_last':
                def _del():
                    res = _fire_and_wait({'mode': 'delete_last'}, timeout=15)
                    if res.get('ok'):
                        _send('success', res.get('msg', 'נמחק'))
                    elif res.get('noop'):
                        _send('system', res.get('msg', ''))   # nothing to delete — neutral, not an error
                    else:
                        _send('error', res.get('msg', 'שגיאה במחיקה'))
                threading.Thread(target=_del, daemon=True).start()

            elif action == 'clear_model':
                def _clear():
                    res = _fire_and_wait({'mode': 'clear_model'}, timeout=30)
                    if res.get('ok'):
                        _send('success', res.get('msg', 'המודל נוקה'))
                    elif res.get('noop'):
                        _send('system', res.get('msg', ''))
                    else:
                        _send('error', res.get('msg', 'שגיאה בניקוי המודל'))
                threading.Thread(target=_clear, daemon=True).start()

            elif action == 'run_direct_code':
                code = data.get('code', '').strip()
                if not code:
                    _send('error', 'אין קוד להרצה')
                    return
                def _run_direct():
                    res = _fire_and_wait({'mode': 'model', 'code': code}, timeout=60)
                    if res.get('ok'):
                        _send('success', f'הקוד רץ בהצלחה! {res.get("msg","")}')
                        if res.get('info', {}).get('params'):
                            _send_params(res['info']['params'])
                    else:
                        _send('error', f'שגיאה: {res.get("msg", "unknown")}')
                threading.Thread(target=_run_direct, daemon=True).start()

            elif action == 'query_model':
                threading.Thread(target=_query_pipeline_thread, args=(data,), daemon=True).start()

            elif action == 'edit_model':
                threading.Thread(target=_edit_pipeline_thread, args=(data,), daemon=True).start()

            elif action == 'manage_params':
                threading.Thread(target=_params_pipeline_thread, args=(data,), daemon=True).start()

            elif action == 'mfg_analysis':
                threading.Thread(target=_mfg_analysis_thread, args=(data,), daemon=True).start()

            elif action == 'gen_drawing':
                threading.Thread(target=_gdt_drawing_pipeline_thread, args=(data,), daemon=True).start()

            elif action == 'explain_timeline':
                threading.Thread(target=_explain_timeline_thread, args=(data,), daemon=True).start()

            elif action == 'set_provider':
                _provider = data.get('provider', 'claude')
                _save_config()
                _send('system', f'Provider: {_provider.upper()}')

            elif action == 'set_api_key':
                _claude_api_key = data.get('key', '').strip()
                _save_config()
                _send('system', 'Claude API key saved' if _claude_api_key else 'API key cleared')

            elif action == 'set_groq_key':
                _groq_api_key = data.get('key', '').strip()
                _save_config()
                _send('system', 'Groq API key saved' if _groq_api_key else 'Groq key cleared')

            elif action == 'set_gemini_key':
                _gemini_api_key = data.get('key', '').strip()
                _save_config()
                _send('system', 'Gemini API key saved' if _gemini_api_key else 'Gemini key cleared')

            elif action == 'test_groq_key':
                t = threading.Thread(target=_test_groq_key_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'test_gemini_key':
                t = threading.Thread(target=_test_gemini_key_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'set_ollama_model':
                _ollama_model = data.get('model', _ollama_model)
                _save_config()
                _send('system', f'Ollama model: {_ollama_model}')

            elif action == 'send_assembly':
                if not _claude_api_key:
                    _send('error', 'מצב הרכבה מצריך Claude API.')
                    return
                # Assemblies now build REAL Components + Joints (movable in Fusion).
                t = threading.Thread(target=_real_assembly_pipeline_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'test_api_key':
                t = threading.Thread(target=_test_api_key_thread, args=(data,), daemon=True)
                t.start()

            elif action == 'get_ollama_models':
                t = threading.Thread(target=_get_ollama_models_thread, daemon=True)
                t.start()

            elif action == 'confirm_plan':
                _confirm_result = True
                _confirm_event.set()

            elif action == 'cancel_plan':
                _confirm_result = False
                _confirm_event.set()

            elif action == 'clear_context':
                _conversation = []
                _last_code    = ''
                _last_params  = {}
                _send('system', 'Conversation context cleared.')

            elif action == 'ready':
                _send('system', 'CAD Assistant v2 ready.')
                # Send saved key/provider to panel
                if _palette:
                    _palette.sendInfoToHTML('restore_config', json.dumps({
                        'provider':     _provider,
                        'has_claude':   bool(_claude_api_key),
                        'ollama_model': _ollama_model,
                    }))
                # Auto-detect Ollama models on startup
                t = threading.Thread(target=_get_ollama_models_thread, daemon=True)
                t.start()

        except Exception:
            _send('error', traceback.format_exc())


# ══════════════════════════════════════════════════════════════
# PALETTE HANDLER
# ══════════════════════════════════════════════════════════════

class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        global _palette
        try:
            pal = _ui.palettes.itemById(PALETTE_ID)
            if pal:
                pal.isVisible = True
                _palette = pal
                return

            html_path = os.path.join(ADDIN_DIR, 'resources', 'panel.html').replace('\\', '/')
            pal = _ui.palettes.add(
                PALETTE_ID, 'CAD Assistant',
                f'file:///{html_path}',
                True, True, True, 440, 720
            )
            pal.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
            pal.isVisible = True
            _palette = pal

            h = HTMLEventHandler()
            pal.incomingFromHTML.add(h)
            _handlers.append(h)

        except Exception:
            _ui.messageBox(traceback.format_exc())


# ══════════════════════════════════════════════════════════════
# RUN / STOP
# ══════════════════════════════════════════════════════════════

def run(context):
    global _app, _ui, _execute_event
    try:
        _app = adsk.core.Application.get()
        _ui  = _app.userInterface
        _load_config()

        # Clean up stale
        for i in range(_ui.allToolbarPanels.count):
            try:
                ctrl = _ui.allToolbarPanels.item(i).controls.itemById('cadAssistantCmd')
                if ctrl:
                    ctrl.deleteMe()
            except Exception:
                pass
        old = _ui.commandDefinitions.itemById('cadAssistantCmd')
        if old:
            old.deleteMe()
        old_pal = _ui.palettes.itemById(PALETTE_ID)
        if old_pal:
            old_pal.deleteMe()

        try:
            _app.unregisterCustomEvent(EXECUTE_EVENT_ID)
        except Exception:
            pass
        _execute_event = _app.registerCustomEvent(EXECUTE_EVENT_ID)
        exec_h = ExecuteCustomEvent()
        _execute_event.add(exec_h)
        _handlers.append(exec_h)

        cmd_def = _ui.commandDefinitions.addButtonDefinition(
            'cadAssistantCmd', 'CAD Assistant', 'AI-powered CAD assistant',
            os.path.join(ADDIN_DIR, 'resources')
        )
        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        # Add button to panel — try Design workspace first, then all workspaces
        panel = None
        for pid in ['SolidScriptsAddinsPanel', 'ToolsAddinsPanel', 'SolidInspectPanel', 'InspectPanel']:
            panel = _ui.allToolbarPanels.itemById(pid)
            if panel:
                break
        if not panel:
            # fallback: first panel that accepts a command
            for i in range(_ui.allToolbarPanels.count):
                try:
                    candidate = _ui.allToolbarPanels.item(i)
                    candidate.controls.addCommand(cmd_def)
                    panel = candidate
                    break
                except Exception:
                    continue
        if panel:
            try:
                panel.controls.addCommand(cmd_def)
            except Exception:
                pass

    except Exception:
        if _ui:
            _ui.messageBox(f'CAD Assistant start failed:\n{traceback.format_exc()}')


def stop(context):
    global _palette, _execute_event
    # UI cleanup — guarded, because run() may have failed before _ui was set.
    if _ui:
        try:
            if _palette:
                _palette.deleteMe()
        except Exception:
            pass
        _palette = None
        for i in range(_ui.allToolbarPanels.count):
            try:
                ctrl = _ui.allToolbarPanels.item(i).controls.itemById('cadAssistantCmd')
                if ctrl:
                    ctrl.deleteMe()
            except Exception:
                pass
        try:
            cmd_def = _ui.commandDefinitions.itemById('cadAssistantCmd')
            if cmd_def:
                cmd_def.deleteMe()
        except Exception:
            pass

    # Always unregister the custom event, even if the UI cleanup above failed —
    # otherwise the event leaks and double-fires on the next reload.
    try:
        if _execute_event:
            _app.unregisterCustomEvent(EXECUTE_EVENT_ID)
    except Exception:
        pass
    _execute_event = None
    _handlers.clear()
